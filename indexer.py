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
        # Scan app/code
        app_code = self.root / "app" / "code"
        if app_code.exists():
            for v in app_code.iterdir():
                if v.is_dir():
                    for m in v.iterdir():
                        if m.is_dir():
                            # Registration.php extraction
                            reg = m / "registration.php"
                            if reg.exists():
                                match = re.search(r"['\"](\w+_\w+)['\"]", reg.read_text())
                                if match:
                                    path_map[match.group(1)] = m.resolve()
                                    continue
                            path_map[f"{v.name}_{m.name}"] = m.resolve()
        
        # Scan vendor via PSR-4 roots for registration.php
        for entry in self.psr4.map:
            reg = entry.path / "registration.php"
            if reg.exists():
                match = re.search(r"['\"](\w+_\w+)['\"]", reg.read_text())
                if match:
                    path_map[match.group(1)] = entry.path

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

    def save_cache(self, cache_file: str = ".indexer_cache.json"):
        """Saves the current index and project metadata to a file."""
        data = {
            "modules": [asdict(m) for m in self.modules],
            "themes": [asdict(t) for t in self.themes],
            "index": [asdict(r) for r in self.index],
            "templates": self.templates
        }
        
        # Helper to convert Path objects to strings for JSON serialization
        def path_serializer(obj):
            if isinstance(obj, Path):
                return str(obj)
            raise TypeError(f"Type {type(obj)} not serializable")

        cache_path = self.root / cache_file
        with open(cache_path, "w") as f:
            json.dump(data, f, default=path_serializer, indent=2)
        print(f"Cache saved to {cache_path}")

    def load_cache(self, cache_file: str = ".indexer_cache.json") -> bool:
        """Loads index and project metadata from a file. Returns True if successful."""
        cache_path = self.root / cache_file
        if not cache_path.exists():
            return False
        
        try:
            with open(cache_path, "r") as f:
                data = json.load(f)
            
            self.modules = [ModuleInfo(m['name'], Path(m['path']), m['order']) for m in data.get('modules', [])]
            self.themes = [ThemeInfo(t['code'], t['area'], Path(t['path']), t.get('parent_code')) for t in data.get('themes', [])]
            self.index = [Reference(**r) for r in data.get('index', [])]
            self.templates = data.get('templates', [])
            return True
        except Exception as e:
            logging.error(f"Failed to load cache: {e}")
            return False

    def save_summary_md(self, summary_file: str = ".indexer_summary.md"):
        """Generates a Markdown summary of the project for LLM context/search."""
        summary_path = self.root / summary_file
        
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
    import sys
    
    root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    print(f"Indexing Magento project at {root}...")
    
    start = time.time()
    indexer = MagentoIndexer(root)
    
    # Try loading from cache first
    if not indexer.load_cache():
        print("No cache found. Performing full scan...")
        indexer.scan()
        indexer.save_cache()
    else:
        print("Loaded from cache.")
    
    # Always generate the MD summary for LLM visibility
    indexer.save_summary_md()
    
    print(f"Index complete: {len(indexer.index)} references found.")
    print(f"Time elapsed: {time.time() - start:.2f} seconds.")
