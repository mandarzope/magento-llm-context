<?php
use Magento\Framework\App\Bootstrap;

require dirname(__DIR__) . '/app/bootstrap.php';

$bootstrap = Bootstrap::create(BP, $_SERVER);
$om = $bootstrap->getObjectManager();

// --- CONFIGURATION ---
$targetNamespace = 'Magento\Catalog'; 
$outputDir = BP . '/docs';
if (!is_dir($outputDir)) {
    mkdir($outputDir, 0777, true);
}

$componentRegistrar = new \Magento\Framework\Component\ComponentRegistrar();
$modulePaths = $componentRegistrar->getPaths(\Magento\Framework\Component\ComponentRegistrar::MODULE);
$areas = ['global', 'frontend', 'adminhtml', 'graphql', 'webapi_rest', 'crontab'];

/**
 * ARCHITECTURAL DEFINITIONS
 */
$definitions = [
    'preferences' => "## Architectural Impact: Preferences\n" .
                     "Preferences are Magento's primary **Dependency Injection** mechanism for interface mapping. " .
                     "When a class requests an Interface in its constructor, the Object Manager uses these rules to " .
                     "decide which concrete Class to instantiate. Re-mapping a preference allows you to globally " .
                     "replace a core service with your own implementation.\n\n",
    
    'plugins'     => "## Architectural Impact: Plugins (Interceptors)\n" .
                     "Plugins allow you to execute code **before, after, or around** any public method of a target class " .
                     "without modifying the original file. They are powered by the Interceptor pattern and are the " .
                     "recommended way to extend business logic while maintaining modularity and compatibility.\n\n",
    
    'vTypes'      => "## Architectural Impact: Virtual Types\n" .
                     "Virtual Types allow you to create a **sub-class alias** entirely within XML. You can take an " .
                     "existing class and inject different constructor arguments into it without creating a new `.php` " .
                     "file on disk. This is heavily used for customizing generic collection processors or adapters.\n\n",
    
    'routes'      => "## Architectural Impact: WebAPI Routes\n" .
                     "WebAPI routes map external HTTP requests (REST/SOAP) to specific **Service Interfaces**. " .
                     "This layer handles the conversion of JSON/XML payloads into PHP objects and enforces " .
                     "ACL permissions before calling the underlying business logic.\n\n"
];

foreach ($areas as $area) {
    echo "Processing EXPLICIT entries for Area: $area...\n";

    $prefs = "| Interface | Implementation |\n|---|---|\n";
    $vTypes = "| Virtual Type Name | Base Class |\n|---|---|\n";
    $plugins = "| Target Class | Plugin Name | Plugin Class |\n|---|---|---|\n";
    $routes = "| Method | Route Path | Service Interface | Service Method |\n|---|---|---|---|\n";

    $hasData = ['prefs' => false, 'vTypes' => false, 'plugins' => false, 'routes' => false];

    foreach ($modulePaths as $moduleName => $path) {
        $subPath = ($area === 'global') ? '/etc/di.xml' : "/etc/$area/di.xml";
        $diFile = $path . $subPath;

        if (file_exists($diFile)) {
            $xml = @simplexml_load_file($diFile);
            if (!$xml) continue;

            foreach ($xml->xpath('//preference') as $pref) {
                $for = (string)$pref['for'];
                $type = (string)$pref['type'];
                if (str_contains($for, $targetNamespace) || str_contains($type, $targetNamespace)) {
                    $prefs .= "| `{$for}` | `{$type}` |\n";
                    $hasData['prefs'] = true;
                }
            }

            foreach ($xml->xpath('//virtualType') as $vt) {
                $name = (string)$vt['name'];
                $type = (string)$vt['type'];
                if (str_contains($name, $targetNamespace)) {
                    $vTypes .= "| `{$name}` | `{$type}` |\n";
                    $hasData['vTypes'] = true;
                }
            }

            foreach ($xml->xpath('//type/plugin') as $plugin) {
                $target = (string)$plugin->xpath('..')[0]['name'];
                $pName = (string)$plugin['name'];
                $pType = (string)$plugin['type'];
                if (str_contains($target, $targetNamespace) || str_contains($pType, $targetNamespace)) {
                    $plugins .= "| `{$target}` | `{$pName}` | `{$pType}` |\n";
                    $hasData['plugins'] = true;
                }
            }
        }

        if (($area === 'webapi_rest' || $area === 'global')) {
            $webapiFile = $path . '/etc/webapi.xml';
            if (file_exists($webapiFile)) {
                $xml = @simplexml_load_file($webapiFile);
                if ($xml) {
                    foreach ($xml->xpath('//route') as $route) {
                        $iface = (string)$route->service['class'];
                        if (str_contains($iface, $targetNamespace)) {
                            $routes .= "| `{$route['method']}` | `{$route['url']}` | `{$iface}` | `{$route->service['method']}` |\n";
                            $hasData['routes'] = true;
                        }
                    }
                }
            }
        }
    }

    // --- WRITE TO DISK ---
    if ($hasData['prefs']) {
        file_put_contents("$outputDir/{$area}_preferences.md", "# Explicit $area Preferences\n" . $definitions['preferences'] . $prefs);
    }
    if ($hasData['vTypes']) {
        file_put_contents("$outputDir/{$area}_virtual_types.md", "# Explicit $area Virtual Types\n" . $definitions['vTypes'] . $vTypes);
    }
    if ($hasData['plugins']) {
        file_put_contents("$outputDir/{$area}_plugins.md", "# Explicit $area Plugins\n" . $definitions['plugins'] . $plugins);
    }
    if ($hasData['routes']) {
        file_put_contents("$outputDir/{$area}_webapi_routes.md", "# Explicit $area WebAPI Routes\n" . $definitions['routes'] . $routes);
    }
}

echo "\nStrict Extraction Complete. Check the 'docs' folder.\n";