import os
import re
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any, Set, Generator
from concurrent.futures import ProcessPoolExecutor
from lxml import etree

# --- Types & Data Structures ---

@dataclass
class ModuleInfo:
    name: str
    path: Path
    order: int

@dataclass
class PSR4Entry:
    prefix: str
    path: Path

@dataclass
class Reference:
    kind: str
    value: str
    file: str
    line: int
    column: int
    end_column: int
    area: str = "global"
    module: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

@dataclass
class MethodInfo:
    name: str
    visibility: str  # public, protected, private
    line: int
    description: str = ""
    params: List[str] = field(default_factory=list)
    return_type: str = ""
    is_static: bool = False
    calls: List[str] = field(default_factory=list)       # ["self::method", "ClassName::method"]
    called_by: List[str] = field(default_factory=list)    # populated in post-processing

@dataclass
class ClassInfo:
    fqcn: str
    file: str
    line: int
    module: str = ""
    parent: str = ""
    interfaces: List[str] = field(default_factory=list)
    methods: List[MethodInfo] = field(default_factory=list)

@dataclass
class ThemeInfo:
    code: str
    area: str
    path: Path
    parent_code: Optional[str] = None

# --- PSR-4 Mapper ---

class PSR4Mapper:
    """
    Port of composerAutoload.ts and phpClassLocator.ts.
    Resolves PHP FQCNs to filesystem paths using PSR-4 mappings.
    """
    def __init__(self, root: Path):
        self.root = root
        self.map: List[PSR4Entry] = []
        self._build_map()

    def _build_map(self):
        # 1. vendor/composer/installed.json
        installed_json = self.root / "vendor" / "composer" / "installed.json"
        if installed_json.exists():
            try:
                with open(installed_json, 'r') as f:
                    data = json.load(f)
                    # Handle both Composer 1 and 2 formats
                    packages = data.get('packages', data) 
                    for pkg in packages:
                        autoload = pkg.get('autoload', {})
                        psr4 = autoload.get('psr-4', {})
                        pkg_path = self.root / "vendor" / pkg['name']
                        for prefix, dirs in psr4.items():
                            if isinstance(dirs, str): dirs = [dirs]
                            for d in dirs:
                                # Resolve path relative to package root
                                target_path = (pkg_path / d).resolve()
                                self.map.append(PSR4Entry(prefix, target_path))
            except Exception as e:
                logging.error(f"Failed to parse installed.json: {e}")

        # 2. app/code (Standard Magento local module location)
        app_code = self.root / "app" / "code"
        if app_code.exists():
            for vendor in app_code.iterdir():
                if vendor.is_dir():
                    for module in vendor.iterdir():
                        if module.is_dir():
                            prefix = f"{vendor.name}\\{module.name}\\"
                            self.map.append(PSR4Entry(prefix, module.resolve()))

        # Sort by prefix length descending for longest-match-first resolution
        self.map.sort(key=lambda x: len(x.prefix), reverse=True)

    def resolve_file_to_fqcn(self, file_path: Path) -> Optional[str]:
        abs_path = file_path.resolve()
        for entry in self.map:
            if str(abs_path).startswith(str(entry.path)):
                rel_path = abs_path.relative_to(entry.path)
                # Convert path segments to namespace segments
                fqcn = entry.prefix + str(rel_path.with_suffix('')).replace(os.sep, '\\')
                return fqcn.strip('\\')
        return None

# --- XML Parser Infrastructure ---

class BaseXMLParser:
    """
    Base class for XML parsing, providing column position detection 
    to replicate xmlPositionUtil.ts functionality.
    """
    def __init__(self):
        self.ns = {"xsi": "http://www.w3.org/2001/XMLSchema-instance"}

    def _get_local_name(self, elem) -> str:
        """Safely gets the local name of an element, skipping comments/PIs."""
        tag = elem.tag
        if not isinstance(tag, str):
            return ""
        return tag.split("}")[-1].lower()

    def get_column_pos(self, line_content: str, value: str, attr_name: Optional[str] = None) -> tuple:
        """Finds the start and end column of a value in a line, scoped by attribute name if provided."""
        if attr_name:
            # Match attribute="value" or attribute='value' with optional whitespace
            pattern = rf'{re.escape(attr_name)}\s*=\s*["\']{re.escape(value)}["\']'
            match = re.search(pattern, line_content)
            if match:
                start = line_content.find(value, match.start())
                return start, start + len(value)
        
        start = line_content.find(value)
        if start != -1:
            return start, start + len(value)
        return 0, 0

    def parse(self, file_path: Path, area: str, module: str) -> List[Reference]:
        refs = []
        try:
            # lxml is faster than standard ElementTree and provides sourceline
            parser = etree.XMLParser(remove_comments=True, recover=True)
            tree = etree.parse(str(file_path), parser=parser)
            lines = file_path.read_text().splitlines()
            
            for elem in tree.iter():
                new_refs = self.handle_element(elem, lines, file_path, area, module)
                if new_refs:
                    refs.extend(new_refs)
        except Exception as e:
            logging.warning(f"Failed to parse {file_path}: {e}")
        return refs

    def handle_element(self, elem, lines: List[str], file: Path, area: str, module: str) -> List[Reference]:
        return []

class DIParser(BaseXMLParser):
    """
    Port of diXmlParser.ts. Extracts preferences, types, and virtualTypes.
    """
    def handle_element(self, elem, lines, file, area, module) -> List[Reference]:
        tag = self._get_local_name(elem)
        if not tag: return []
        refs = []
        
        mappings = {
            'preference': [('for', 'preference-for'), ('type', 'preference-type')],
            'type': [('name', 'type-name')],
            'plugin': [('type', 'plugin-type')],
            'virtualtype': [('name', 'virtualtype-name'), ('type', 'virtualtype-type')]
        }

        if tag in mappings:
            for attr, kind in mappings[tag]:
                val = elem.get(attr)
                if val:
                    line_idx = elem.sourceline - 1
                    col, end_col = self.get_column_pos(lines[line_idx], val, attr)
                    refs.append(Reference(kind, val, str(file), line_idx, col, end_col, area, module))
        
        elif tag in ('argument', 'item'):
            xsi_type = elem.get(f"{{{self.ns['xsi']}}}type")
            if (xsi_type == 'object' or xsi_type == 'string') and elem.text:
                val = elem.text.strip()
                if not val: return []
                
                if xsi_type == 'string':
                    if '\\' not in val or not re.match(r'^\\?[A-Za-z_][A-Za-z0-9_\\]*$', val):
                        return []

                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], val)
                kind = 'argument-object' if xsi_type == 'object' else 'argument-string'
                refs.append(Reference(kind, val, str(file), line_idx, col, end_col, area, module))
                
        return refs

class EventsParser(BaseXMLParser):
    def __init__(self):
        super().__init__()
        self.current_event = None

    def parse(self, file_path: Path, area: str, module: str) -> List[Reference]:
        self.current_event = None
        return super().parse(file_path, area, module)

    def handle_element(self, elem, lines, file, area, module) -> List[Reference]:
        tag = self._get_local_name(elem)
        if not tag: return []
        refs = []
        if tag == 'event':
            self.current_event = elem.get('name')
            if self.current_event:
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], self.current_event, 'name')
                refs.append(Reference('event-name', self.current_event, str(file), line_idx, col, end_col, area, module))
        elif tag == 'observer' and self.current_event:
            instance = elem.get('instance')
            if instance:
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], instance, 'instance')
                refs.append(Reference('observer-instance', instance, str(file), line_idx, col, end_col, area, module, 
                                     extra={'event': self.current_event}))
        return refs

class WebapiParser(BaseXMLParser):
    def __init__(self):
        super().__init__()
        self.current_route = None

    def parse(self, file_path: Path, area: str, module: str) -> List[Reference]:
        self.current_route = None
        return super().parse(file_path, area, module)

    def handle_element(self, elem, lines, file, area, module) -> List[Reference]:
        tag = self._get_local_name(elem)
        if not tag: return []
        refs = []
        if tag == 'route':
            self.current_route = {'url': elem.get('url'), 'method': elem.get('method')}
        elif self.current_route:
            if tag == 'service':
                cls = elem.get('class')
                method = elem.get('method')
                if cls:
                    line_idx = elem.sourceline - 1
                    col, end_col = self.get_column_pos(lines[line_idx], cls, 'class')
                    refs.append(Reference('service-class', cls, str(file), line_idx, col, end_col, area, module, extra=self.current_route))
                if method:
                    line_idx = elem.sourceline - 1
                    col, end_col = self.get_column_pos(lines[line_idx], method, 'method')
                    refs.append(Reference('service-method', method, str(file), line_idx, col, end_col, area, module, extra=self.current_route))
            elif tag == 'resource':
                ref = elem.get('ref')
                if ref:
                    line_idx = elem.sourceline - 1
                    col, end_col = self.get_column_pos(lines[line_idx], ref, 'ref')
                    refs.append(Reference('resource-ref', ref, str(file), line_idx, col, end_col, area, module, extra=self.current_route))
        return refs

class LayoutParser(BaseXMLParser):
    def __init__(self):
        super().__init__()
        self.block_stack = []

    def parse(self, file_path: Path, area: str, module: str) -> List[Reference]:
        self.block_stack = []
        return super().parse(file_path, area, module)

    def handle_element(self, elem, lines, file, area, module) -> List[Reference]:
        tag = self._get_local_name(elem)
        if not tag: return []
        refs = []
        
        if tag == 'block':
            cls = elem.get('class') or ''
            self.block_stack.append(cls)
            if cls:
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], cls, 'class')
                refs.append(Reference('block-class', cls, str(file), line_idx, col, end_col, area, module))
            
            template = elem.get('template')
            if template:
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], template, 'template')
                refs.append(Reference('block-template', template, str(file), line_idx, col, end_col, area, module))

        elif tag == 'referenceblock':
            self.block_stack.append('') # Dummy for stack balance
            name = elem.get('name')
            if name:
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], name, 'name')
                refs.append(Reference('reference-block', name, str(file), line_idx, col, end_col, area, module))
            
            template = elem.get('template')
            if template:
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], template, 'template')
                refs.append(Reference('refblock-template', template, str(file), line_idx, col, end_col, area, module))

        elif tag == 'update':
            handle = elem.get('handle')
            if handle:
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], handle, 'handle')
                refs.append(Reference('update-handle', handle, str(file), line_idx, col, end_col, area, module))

        return refs

class SimpleAttrParser(BaseXMLParser):
    """Generic parser for simple attribute-based references (ACL, Menu, Routes, etc.)."""
    def __init__(self, tag_map: Dict[str, List[tuple]]):
        super().__init__()
        self.tag_map = tag_map

    def handle_element(self, elem, lines, file, area, module) -> List[Reference]:
        tag = self._get_local_name(elem)
        if not tag: return []
        refs = []
        if tag in self.tag_map:
            for attr, kind in self.tag_map[tag]:
                val = elem.get(attr)
                if val:
                    line_idx = elem.sourceline - 1
                    col, end_col = self.get_column_pos(lines[line_idx], val, attr)
                    refs.append(Reference(kind, val, str(file), line_idx, col, end_col, area, module))
        return refs

class SystemConfigParser(BaseXMLParser):
    def handle_element(self, elem, lines, file, area, module) -> List[Reference]:
        tag = self._get_local_name(elem)
        if not tag: return []
        refs = []
        if tag in ('source_model', 'backend_model', 'frontend_model'):
            if elem.text:
                val = elem.text.strip()
                line_idx = elem.sourceline - 1
                col, end_col = self.get_column_pos(lines[line_idx], val)
                refs.append(Reference(f'system-{tag.replace("_", "-")}', val, str(file), line_idx, col, end_col, area, module))
        return refs

# --- Main Project Indexer ---

class MagentoIndexer:
    """
    High-level manager for the indexing process.
    Port of projectManager.ts and moduleResolver.ts.
    """
    def __init__(self, root_path: str):
        self.root = Path(root_path).resolve()
        self.modules: List[ModuleInfo] = []
        self.themes: List[ThemeInfo] = []
        self.psr4 = PSR4Mapper(self.root)
        self.index: List[Reference] = []
        self.templates: List[Dict[str, Any]] = []
        self.classes: List[ClassInfo] = []
        self._load_modules()
        self._load_themes()

    def _load_modules(self):
        """Discovers active modules by parsing app/etc/config.php."""
        config_php = self.root / "app" / "etc" / "config.php"
        if not config_php.exists():
            return

        content = config_php.read_text()
        module_entries = re.findall(r"['\"](\w+_\w+)['\"]\s*=>\s*1", content)
        
        path_map = {}
        # Scan vendor first so app/code can override on conflict
        for entry in self.psr4.map:
            reg = entry.path / "registration.php"
            if reg.exists():
                match = re.search(r"['\"](\w+_\w+)['\"]", reg.read_text())
                if match:
                    path_map[match.group(1)] = entry.path

        # Scan app/code (takes priority over vendor)
        app_code = self.root / "app" / "code"
        if app_code.exists():
            for v in app_code.iterdir():
                if v.is_dir():
                    for m in v.iterdir():
                        if m.is_dir():
                            reg = m / "registration.php"
                            if reg.exists():
                                match = re.search(r"['\"](\w+_\w+)['\"]", reg.read_text())
                                if match:
                                    path_map[match.group(1)] = m.resolve()
                                    continue
                            path_map[f"{v.name}_{m.name}"] = m.resolve()

        for i, name in enumerate(module_entries):
            if name in path_map:
                self.modules.append(ModuleInfo(name, path_map[name], i))

    def _load_themes(self):
        """Discovers all themes from app/design/ and vendor/."""
        # 1. app/design/frontend/Vendor/name and app/design/adminhtml/Vendor/name
        design_root = self.root / "app" / "design"
        if design_root.exists():
            for area in ['frontend', 'adminhtml']:
                area_dir = design_root / area
                if area_dir.exists():
                    for vendor in area_dir.iterdir():
                        if vendor.is_dir():
                            for t in vendor.iterdir():
                                if t.is_dir():
                                    theme_xml = t / "theme.xml"
                                    parent = None
                                    if theme_xml.exists():
                                        match = re.search(r"<parent>(.*)</parent>", theme_xml.read_text())
                                        if match: parent = match.group(1)
                                    self.themes.append(ThemeInfo(f"{area}/{vendor.name}/{t.name}", area, t.resolve(), parent))

        # 2. vendor packages with type "magento2-theme"
        installed_json = self.root / "vendor" / "composer" / "installed.json"
        if installed_json.exists():
            try:
                with open(installed_json, 'r') as f:
                    data = json.load(f)
                    packages = data.get('packages', data)
                    for pkg in packages:
                        if pkg.get('type') == 'magento2-theme':
                            pkg_path = self.root / "vendor" / pkg['name']
                            reg = pkg_path / "registration.php"
                            if reg.exists():
                                match = re.search(r"THEME\s*,\s*'((frontend|adminhtml)/[\w-]+/[\w-]+)'", reg.read_text())
                                if match:
                                    code = match.group(1)
                                    area = code.split('/')[0]
                                    theme_xml = pkg_path / "theme.xml"
                                    parent = None
                                    if theme_xml.exists():
                                        match_p = re.search(r"<parent>(.*)</parent>", theme_xml.read_text())
                                        if match_p: parent = match_p.group(1)
                                    self.themes.append(ThemeInfo(code, area, pkg_path.resolve(), parent))
            except:
                pass

    def scan(self):
        """Discovers and parses all relevant Magento XML files."""
        tasks = []
        
        # Parsers
        di_parser = DIParser()
        events_parser = EventsParser()
        webapi_parser = WebapiParser()
        layout_parser = LayoutParser()
        acl_parser = SimpleAttrParser({'resource': [('id', 'acl-resource')]})
        menu_parser = SimpleAttrParser({'add': [('resource', 'menu-resource')], 'update': [('resource', 'menu-resource')]})
        routes_parser = SimpleAttrParser({'route': [('id', 'route-id'), ('frontname', 'route-frontname')], 'module': [('name', 'route-module')]})
        db_schema_parser = SimpleAttrParser({'table': [('name', 'table-name')], 'column': [('name', 'column-name')]})
        ui_acl_parser = SimpleAttrParser({'aclresource': [('id', 'ui-acl-resource')]})
        system_parser = SystemConfigParser()

        # Root files
        root_di = self.root / "app" / "etc" / "di.xml"
        if root_di.exists(): tasks.append((di_parser, root_di, "global", "__root__"))
        
        for mod in self.modules:
            # etc/ files
            for f, area in self._discover_xml(mod.path, "di.xml"): tasks.append((di_parser, f, area, mod.name))
            for f, area in self._discover_xml(mod.path, "events.xml"): tasks.append((events_parser, f, area, mod.name))
            for f, area in self._discover_xml(mod.path, "webapi.xml"): tasks.append((webapi_parser, f, area, mod.name))
            for f, area in self._discover_xml(mod.path, "acl.xml"): tasks.append((acl_parser, f, area, mod.name))
            for f, area in self._discover_xml(mod.path, "routes.xml"): tasks.append((routes_parser, f, area, mod.name))
            for f, area in self._discover_xml(mod.path, "db_schema.xml"): tasks.append((db_schema_parser, f, area, mod.name))
            
            # menu.xml (adminhtml only)
            menu_xml = mod.path / "etc" / "adminhtml" / "menu.xml"
            if menu_xml.exists(): tasks.append((menu_parser, menu_xml, "adminhtml", mod.name))
            
            # system.xml (adminhtml only)
            system_xml = mod.path / "etc" / "adminhtml" / "system.xml"
            if system_xml.exists(): tasks.append((system_parser, system_xml, "adminhtml", mod.name))

            # Layout files (Module view/)
            view_dir = mod.path / "view"
            if view_dir.exists():
                for area_dir in view_dir.iterdir():
                    if not area_dir.is_dir(): continue
                    for subdir in ['layout', 'page_layout']:
                        target_dir = area_dir / subdir
                        if target_dir.exists():
                            for f in target_dir.glob("*.xml"):
                                tasks.append((layout_parser, f, area_dir.name, mod.name))
                    
                    # UI Components
                    ui_dir = area_dir / "ui_component"
                    if ui_dir.exists():
                        for f in ui_dir.glob("*.xml"):
                            tasks.append((ui_acl_parser, f, area_dir.name, mod.name))

            # Scan for templates in Module view/
            self._scan_templates(mod.path, mod.name)
            
            # Scan for compat modules (Hyva)
            self._scan_compat_modules(mod.path, mod.name)

        # 4. Scan Themes for overrides
        for theme in self.themes:
            # Layout overrides: {themePath}/{Module_Name}/layout/*.xml
            for mod_dir in theme.path.iterdir():
                if mod_dir.is_dir() and "_" in mod_dir.name:
                    mod_name = mod_dir.name
                    for subdir in ['layout', 'page_layout']:
                        target_dir = mod_dir / subdir
                        if target_dir.exists():
                            for f in target_dir.glob("*.xml"):
                                tasks.append((layout_parser, f, theme.area, mod_name))
                    
                    # Template overrides: {themePath}/{Module_Name}/templates/*.phtml
                    templates_dir = mod_dir / "templates"
                    if templates_dir.exists():
                        for f in templates_dir.rglob("*.phtml"):
                            rel_path = f.relative_to(templates_dir)
                            template_id = f"{mod_name}::{rel_path}"
                            self.templates.append({
                                'id': template_id,
                                'file': str(f),
                                'area': theme.area,
                                'theme': theme.code
                            })

        # Use ProcessPoolExecutor for parallel parsing
        with ProcessPoolExecutor() as executor:
            futures = [executor.submit(parser.parse, f, area, mod_name) for parser, f, area, mod_name in tasks]
            for f in futures:
                self.index.extend(f.result())

        # Scan PHP classes, methods, and call graph
        self.scan_php_classes()

    def scan_classes(self) -> List[str]:
        """Performs a full filesystem walk to find all PHP classes in PSR-4 roots."""
        classes = []
        for entry in self.psr4.map:
            if not entry.path.exists(): continue
            for f in entry.path.rglob("*.php"):
                fqcn = self.psr4.resolve_file_to_fqcn(f)
                if fqcn:
                    classes.append(fqcn)
        return classes

    def scan_php_classes(self):
        """Parses PHP files to extract classes, methods, docblocks, and call graphs."""
        self.classes: List[ClassInfo] = []
        php_files = []

        for mod in self.modules:
            if not mod.path.exists():
                continue
            for f in mod.path.rglob("*.php"):
                if f.name == 'registration.php':
                    continue
                php_files.append((f, mod.name))

        for file_path, module_name in php_files:
            try:
                content = file_path.read_text(errors='replace')
                parsed = self._parse_php_file(content, file_path, module_name)
                self.classes.extend(parsed)
            except Exception as e:
                logging.warning(f"Failed to parse PHP {file_path}: {e}")

        self._build_call_graph()
        print(f"PHP class scan: {len(self.classes)} classes, "
              f"{sum(len(c.methods) for c in self.classes)} methods indexed.")

    def _parse_php_file(self, content: str, file_path: Path, module_name: str) -> List[ClassInfo]:
        """Extracts class declarations, methods, and calls from a PHP file."""
        lines = content.split('\n')
        results = []

        # Resolve namespace
        ns_match = re.search(r'^\s*namespace\s+([\w\\]+)\s*;', content, re.MULTILINE)
        namespace = ns_match.group(1) if ns_match else ''

        # Collect use imports for resolving short class names in calls
        use_map = {}
        for m in re.finditer(r'^\s*use\s+([\w\\]+?)(?:\s+as\s+(\w+))?\s*;', content, re.MULTILINE):
            fqcn = m.group(1)
            alias = m.group(2) or fqcn.rsplit('\\', 1)[-1]
            use_map[alias] = fqcn

        # Find class/interface/trait declarations
        class_pattern = re.compile(
            r'^(?:abstract\s+)?(?:final\s+)?(?:class|interface|trait)\s+(\w+)'
            r'(?:\s+extends\s+([\w\\]+))?'
            r'(?:\s+implements\s+([\w\\,\s]+))?',
            re.MULTILINE
        )

        for cls_match in class_pattern.finditer(content):
            class_name = cls_match.group(1)
            parent_short = cls_match.group(2) or ''
            implements_raw = cls_match.group(3) or ''

            fqcn = f"{namespace}\\{class_name}" if namespace else class_name
            parent_fqcn = self._resolve_short_name(parent_short, namespace, use_map) if parent_short else ''
            interfaces = [
                self._resolve_short_name(i.strip(), namespace, use_map)
                for i in implements_raw.split(',') if i.strip()
            ]

            cls_line = content[:cls_match.start()].count('\n')

            # Find the class body boundaries
            body_start = content.find('{', cls_match.end())
            if body_start == -1:
                continue
            body_end = self._find_matching_brace(content, body_start)
            if body_end == -1:
                body_end = len(content)

            class_body = content[body_start:body_end + 1]
            body_offset = body_start

            methods = self._extract_methods(class_body, body_offset, lines, fqcn, namespace, use_map)

            results.append(ClassInfo(
                fqcn=fqcn,
                file=str(file_path),
                line=cls_line,
                module=module_name,
                parent=parent_fqcn,
                interfaces=interfaces,
                methods=methods
            ))

        return results

    def _resolve_short_name(self, name: str, namespace: str, use_map: Dict[str, str]) -> str:
        """Resolves a short/aliased class name to a fully qualified name."""
        if name.startswith('\\'):
            return name.lstrip('\\')
        first_part = name.split('\\')[0]
        if first_part in use_map:
            return use_map[first_part] + name[len(first_part):]
        if namespace:
            return f"{namespace}\\{name}"
        return name

    def _find_matching_brace(self, content: str, start: int) -> int:
        """Finds the position of the matching closing brace, skipping strings and comments."""
        depth = 0
        i = start
        in_single_quote = False
        in_double_quote = False
        in_line_comment = False
        in_block_comment = False

        while i < len(content):
            ch = content[i]
            next_ch = content[i + 1] if i + 1 < len(content) else ''

            if in_line_comment:
                if ch == '\n':
                    in_line_comment = False
            elif in_block_comment:
                if ch == '*' and next_ch == '/':
                    in_block_comment = False
                    i += 1
            elif in_single_quote:
                if ch == '\\':
                    i += 1
                elif ch == "'":
                    in_single_quote = False
            elif in_double_quote:
                if ch == '\\':
                    i += 1
                elif ch == '"':
                    in_double_quote = False
            else:
                if ch == '/' and next_ch == '/':
                    in_line_comment = True
                    i += 1
                elif ch == '/' and next_ch == '*':
                    in_block_comment = True
                    i += 1
                elif ch == "'":
                    in_single_quote = True
                elif ch == '"':
                    in_double_quote = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return -1

    def _extract_methods(self, class_body: str, body_offset: int, file_lines: List[str],
                         class_fqcn: str, namespace: str, use_map: Dict[str, str]) -> List[MethodInfo]:
        """Extracts methods from a class body string."""
        methods = []

        method_pattern = re.compile(
            r'(/\*\*.*?\*/\s*)?'  # optional docblock
            r'^[ \t]*(public|protected|private)\s+'
            r'(static\s+)?'
            r'function\s+(\w+)\s*\(([^)]*)\)'
            r'(?:\s*:\s*([\w\\|?]+))?',
            re.MULTILINE | re.DOTALL
        )

        for m in method_pattern.finditer(class_body):
            docblock = m.group(1) or ''
            visibility = m.group(2)
            is_static = bool(m.group(3))
            method_name = m.group(4)
            params_raw = m.group(5)
            return_type = m.group(6) or ''

            abs_pos = body_offset + m.start()
            line_num = file_lines[:abs_pos].count('\n') if abs_pos > 0 else 0

            # Parse description from docblock
            description = self._parse_docblock_description(docblock)

            # Parse params
            params = [p.strip() for p in params_raw.split(',') if p.strip()] if params_raw.strip() else []

            # Find method body to extract calls
            func_body_start = class_body.find('{', m.end())
            if func_body_start == -1:
                calls = []
            else:
                func_body_end = self._find_matching_brace(class_body, func_body_start)
                if func_body_end == -1:
                    func_body_end = len(class_body)
                method_body = class_body[func_body_start:func_body_end + 1]
                calls = self._extract_calls(method_body, class_fqcn, namespace, use_map)

            methods.append(MethodInfo(
                name=method_name,
                visibility=visibility,
                line=line_num,
                description=description,
                params=params,
                return_type=return_type,
                is_static=is_static,
                calls=calls,
            ))

        return methods

    def _parse_docblock_description(self, docblock: str) -> str:
        """Extracts the text description from a PHPDoc block (before any @tags)."""
        if not docblock:
            return ''
        # Remove /** and */ markers, strip leading * from each line
        lines = []
        for line in docblock.split('\n'):
            line = line.strip()
            line = re.sub(r'^/?\*+/?', '', line).strip()
            if line.startswith('@'):
                break
            if line:
                lines.append(line)
        return ' '.join(lines)

    def _extract_calls(self, method_body: str, class_fqcn: str, namespace: str,
                       use_map: Dict[str, str]) -> List[str]:
        """Extracts method calls from a method body, returning qualified references."""
        calls = set()

        # $this->method(
        for m in re.finditer(r'\$this\s*->\s*(\w+)\s*\(', method_body):
            calls.add(f"self::{m.group(1)}")

        # parent::method(
        for m in re.finditer(r'parent\s*::\s*(\w+)\s*\(', method_body):
            calls.add(f"parent::{m.group(1)}")

        # static::method( or self::method(
        for m in re.finditer(r'(?:static|self)\s*::\s*(\w+)\s*\(', method_body):
            calls.add(f"self::{m.group(1)}")

        # ClassName::method( (static calls)
        for m in re.finditer(r'([A-Z]\w+(?:\\[A-Z]\w+)*)\s*::\s*(\w+)\s*\(', method_body):
            cls_name = m.group(1)
            method_name = m.group(2)
            if cls_name in ('self', 'static', 'parent'):
                continue
            resolved = self._resolve_short_name(cls_name, namespace, use_map)
            calls.add(f"{resolved}::{method_name}")

        # $variable->method( — tracked as unresolved for now
        for m in re.finditer(r'\$(\w+)\s*->\s*(\w+)\s*\(', method_body):
            var = m.group(1)
            method_name = m.group(2)
            if var == 'this':
                continue
            calls.add(f"${var}->{method_name}")

        return sorted(calls)

    def _build_call_graph(self):
        """Post-processes classes to populate called_by relationships."""
        # Build a lookup: fqcn::method -> MethodInfo
        method_lookup: Dict[str, MethodInfo] = {}
        for cls in self.classes:
            for method in cls.methods:
                key = f"{cls.fqcn}::{method.name}"
                method_lookup[key] = method

        # Resolve self:: calls and populate called_by
        for cls in self.classes:
            for method in cls.methods:
                caller_key = f"{cls.fqcn}::{method.name}"
                resolved_calls = []
                for call in method.calls:
                    if call.startswith("self::"):
                        resolved = f"{cls.fqcn}::{call.split('::', 1)[1]}"
                    elif call.startswith("parent::") and cls.parent:
                        resolved = f"{cls.parent}::{call.split('::', 1)[1]}"
                    else:
                        resolved = call
                    resolved_calls.append(resolved)

                    # Populate called_by on the target
                    if resolved in method_lookup:
                        target = method_lookup[resolved]
                        if caller_key not in target.called_by:
                            target.called_by.append(caller_key)

                method.calls = resolved_calls

    def _scan_compat_modules(self, module_path: Path, module_name: str):
        """
        Scans frontend/di.xml for Hyva compatibility module registrations.
        Port of compatModuleParser.ts.
        """
        di_xml = module_path / "etc" / "frontend" / "di.xml"
        if not di_xml.exists(): return
        
        try:
            tree = etree.parse(str(di_xml))
            # Look for Hyva\Compatibility\Model\Config\ConfigurableCompatModuleList::add
            for type_elem in tree.xpath("//type[@name='Hyva\\Compatibility\\Model\\Config\\ConfigurableCompatModuleList']"):
                for item in type_elem.xpath(".//argument[@name='compatModules']//item[@xsi:type='string']"):
                    original = item.get('name')
                    compat = item.text.strip() if item.text else None
                    if original and compat:
                        self.index.append(Reference('hyva-compat', compat, str(di_xml), item.sourceline - 1, 0, 0, 
                                                   area='frontend', module=module_name, extra={'original': original}))
        except:
            pass

    def _discover_xml(self, module_path: Path, filename: str) -> List[tuple]:
        results = []
        etc = module_path / "etc"
        if not etc.exists(): return []
        
        global_file = etc / filename
        if global_file.exists():
            results.append((global_file, "global"))
            
        for area_dir in etc.iterdir():
            if area_dir.is_dir():
                area_file = area_dir / filename
                if area_file.exists():
                    results.append((area_file, area_dir.name))
        return results

    def _scan_templates(self, module_path: Path, module_name: str):
        """Finds all .phtml templates in the module."""
        view_dir = module_path / "view"
        if not view_dir.exists(): return
        
        for area_dir in view_dir.iterdir():
            if not area_dir.is_dir(): continue
            templates_dir = area_dir / "templates"
            if templates_dir.exists():
                for f in templates_dir.rglob("*.phtml"):
                    rel_path = f.relative_to(templates_dir)
                    template_id = f"{module_name}::{rel_path}"
                    self.templates.append({
                        'id': template_id,
                        'file': str(f),
                        'area': area_dir.name,
                        'module': module_name
                    })

    def get_index(self):
        """Returns the complete index as a list of dictionaries."""
        return [asdict(r) for r in self.index]

    def save_cache(self, cache_file: str = "indexer_cache.json"):
        """Saves the current index and project metadata as split JSON files."""

        # Helper to convert Path objects to root-relative strings
        def path_to_rel(obj):
            if isinstance(obj, dict):
                return {k: path_to_rel(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [path_to_rel(i) for i in obj]
            elif isinstance(obj, Path):
                try:
                    return str(obj.relative_to(self.root))
                except ValueError:
                    return str(obj)
            elif isinstance(obj, str) and obj.startswith(str(self.root)):
                return str(Path(obj).relative_to(self.root))
            return obj

        cache_dir = self.root / ".code_graph" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Save modules
        with open(cache_dir / "modules.json", "w") as f:
            json.dump(path_to_rel([asdict(m) for m in self.modules]), f, indent=2)

        # Save themes
        with open(cache_dir / "themes.json", "w") as f:
            json.dump(path_to_rel([asdict(t) for t in self.themes]), f, indent=2)

        # Save templates
        with open(cache_dir / "templates.json", "w") as f:
            json.dump(path_to_rel(self.templates), f, indent=2)

        # Save index split by kind
        index_dir = cache_dir / "index"
        index_dir.mkdir(parents=True, exist_ok=True)

        kind_wise_index = {}
        for r in self.index:
            kind = r.kind
            if kind not in kind_wise_index:
                kind_wise_index[kind] = []
            kind_wise_index[kind].append(asdict(r))

        # Write a manifest of index kinds
        with open(index_dir / "_kinds.json", "w") as f:
            json.dump(list(kind_wise_index.keys()), f, indent=2)

        for kind, refs in kind_wise_index.items():
            with open(index_dir / f"{kind}.json", "w") as f:
                json.dump(path_to_rel(refs), f, indent=2)

        # Save classes split by module
        classes_dir = cache_dir / "classes"
        classes_dir.mkdir(parents=True, exist_ok=True)

        module_classes: Dict[str, list] = {}
        for cls in self.classes:
            mod = cls.module or "__unknown__"
            if mod not in module_classes:
                module_classes[mod] = []
            module_classes[mod].append(asdict(cls))

        with open(classes_dir / "_modules.json", "w") as f:
            json.dump(list(module_classes.keys()), f, indent=2)

        for mod, cls_list in module_classes.items():
            safe_name = mod.replace('\\', '_').replace('/', '_')
            with open(classes_dir / f"{safe_name}.json", "w") as f:
                json.dump(path_to_rel(cls_list), f, indent=2)

        print(f"Cache saved to {cache_dir}/")

    def load_cache(self, cache_file: str = "indexer_cache.json") -> bool:
        """Loads index and project metadata from split JSON files."""
        cache_dir = self.root / ".code_graph" / "cache"
        if not cache_dir.exists():
            return False

        try:
            with open(cache_dir / "modules.json", "r") as f:
                self.modules = [ModuleInfo(m['name'], self.root / m['path'], m['order']) for m in json.load(f)]

            with open(cache_dir / "themes.json", "r") as f:
                self.themes = [ThemeInfo(t['code'], t['area'], self.root / t['path'], t.get('parent_code')) for t in json.load(f)]

            with open(cache_dir / "templates.json", "r") as f:
                self.templates = json.load(f)

            # Load index from per-kind files
            index_dir = cache_dir / "index"
            with open(index_dir / "_kinds.json", "r") as f:
                kinds = json.load(f)

            self.index = []
            for kind in kinds:
                with open(index_dir / f"{kind}.json", "r") as f:
                    for r in json.load(f):
                        self.index.append(Reference(**r))

            # Load classes
            classes_dir = cache_dir / "classes"
            if classes_dir.exists():
                with open(classes_dir / "_modules.json", "r") as f:
                    mod_names = json.load(f)
                self.classes = []
                for mod in mod_names:
                    safe_name = mod.replace('\\', '_').replace('/', '_')
                    with open(classes_dir / f"{safe_name}.json", "r") as f:
                        for c in json.load(f):
                            c['methods'] = [MethodInfo(**m) for m in c.get('methods', [])]
                            self.classes.append(ClassInfo(**c))

            return True
        except Exception as e:
            logging.error(f"Failed to load cache: {e}")
            return False

    def save_summary_md(self, summary_file: str = "indexer_summary.md"):
        """Generates a Markdown summary of the project for LLM context/search."""
        summary_path = self.root / ".code_graph" / summary_file
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        
        kinds_count = {}
        for r in self.index:
            kinds_count[r.kind] = kinds_count.get(r.kind, 0) + 1

        md = [
            f"# Magento Project Index Summary",
            f"**Root:** `{self.root}`",
            f"",
            f"## Project Statistics",
            f"- **Active Modules:** {len(self.modules)}",
            f"- **Themes:** {len(self.themes)}",
            f"- **Total DI/XML References:** {len(self.index)}",
            f"- **Templates (.phtml):** {len(self.templates)}",
            f"",
            f"## Reference Breakdown",
        ]
        
        for kind, count in sorted(kinds_count.items()):
            md.append(f"- **{kind}:** {count}")

        md.extend([
            f"",
            f"## Active Modules (Top 20 by Load Order)",
        ])
        for mod in sorted(self.modules, key=lambda x: x.order)[:20]:
            md.append(f"- `{mod.name}` ({mod.path.relative_to(self.root) if self.root in mod.path.parents else mod.path})")

        if self.themes:
            md.extend([f"", f"## Discovered Themes"])
            for theme in self.themes:
                md.append(f"- `{theme.code}` (Area: {theme.area}, Parent: {theme.parent_code or 'None'})")

        # Add a section for key interface preferences (first 10)
        prefs = [r for r in self.index if r.kind == 'preference-for'][:10]
        if prefs:
            md.extend([f"", f"## Key DI Preferences (Sample)"])
            for p in prefs:
                md.append(f"- Interface: `{p.value}` (Module: {p.module})")

        with open(summary_path, "w") as f:
            f.write("\n".join(md))
        print(f"LLM Summary saved to {summary_path}")

if __name__ == "__main__":
    import time
    import argparse

    parser = argparse.ArgumentParser(description="Magento Project Indexer for LLM Context")
    parser.add_argument("--cwd", default=os.getcwd(), help="Root directory of the Magento project (default: current directory)")
    parser.add_argument("--force", action="store_true", help="Force a full scan even if a cache exists")
    parser.add_argument("--cache", default="indexer_cache.json", help="Cache file name (default: indexer_cache.json)")
    parser.add_argument("--summary", default="indexer_summary.md", help="Summary file name (default: indexer_summary.md)")
    
    args = parser.parse_args()
    
    root = args.cwd
    print(f"Indexing Magento project at {root}...")
    
    start = time.time()
    indexer = MagentoIndexer(root)
    
    # Try loading from cache first, unless --force is used
    loaded_from_cache = False
    if not args.force:
        if indexer.load_cache(args.cache):
            print(f"Loaded from cache: {args.cache}")
            loaded_from_cache = True
    
    if not loaded_from_cache:
        print("Performing full scan...")
        indexer.scan()
        indexer.save_cache(args.cache)
    
    # Always generate the MD summary for LLM visibility
    indexer.save_summary_md(args.summary)
    
    print(f"Index complete: {len(indexer.index)} references found.")
    print(f"Time elapsed: {time.time() - start:.2f} seconds.")
