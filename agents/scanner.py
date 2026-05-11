import os
import re
import json
import logging
import argparse
from pathlib import Path
import tomllib

import xml.etree.ElementTree as ET

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

class ScannerAgent:
    def __init__(self, root_dir: str):
        self.root_dir = Path(root_dir)
        self.supported_files = {
            "requirements.txt": self._parse_requirements,
            "pyproject.toml": self._parse_pyproject,
            "package.json": self._parse_package_json,
            "go.mod": self._parse_go_mod,
            "Cargo.toml": self._parse_cargo_toml,
            "pom.xml": self._parse_pom_xml
        }

    def scan(self) -> list[dict]:
        results = []
        if not self.root_dir.exists() or not self.root_dir.is_dir():
            logger.error(f"Invalid directory: {self.root_dir}")
            return results

        for filepath in self.root_dir.rglob("*"):
            if filepath.name in self.supported_files and filepath.is_file():
                # Skip some common directories that we don't want to scan
                if any(part.startswith('.') or part in ['node_modules', 'venv', 'env', '__pycache__', 'target', 'build'] for part in filepath.parts):
                    continue

                try:
                    packages = self.supported_files[filepath.name](filepath)
                    if packages:
                        ecosystem = self._get_ecosystem(filepath.name)
                        results.append({
                            "file_path": str(filepath),
                            "ecosystem": ecosystem,
                            "packages": packages
                        })
                except Exception as e:
                    logger.warning(f"Error parsing {filepath}: {e}")

        return results

    def _get_ecosystem(self, filename: str) -> str:
        mapping = {
            "requirements.txt": "pip",
            "pyproject.toml": "pip",
            "package.json": "npm",
            "go.mod": "go",
            "Cargo.toml": "cargo",
            "pom.xml": "maven"
        }
        return mapping.get(filename, "unknown")

    def _parse_requirements(self, filepath: Path) -> list[dict]:
        packages = []
        # Regex matches name and optional version specifier: django==3.2.18, numpy>=1.0, requests
        pattern = re.compile(r'^([a-zA-Z0-9_\-\.]+)(?:\s*(?:==|>=|<=|~=|>|<)\s*([0-9a-zA-Z\.\-\+]+))?', re.IGNORECASE)
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.split('#')[0].strip() # Remove comments
                if not line or line.startswith('-'):
                    continue
                match = pattern.match(line)
                if match:
                    name = match.group(1)
                    version = match.group(2) or "unknown"
                    packages.append({"name": name, "version": version})
        return packages

    def _parse_pyproject(self, filepath: Path) -> list[dict]:
        packages = []
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        if tomllib:
            try:
                data = tomllib.loads(content)
                deps = data.get("project", {}).get("dependencies", [])
                poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
                
                # Parse standard PEP 621 dependencies (array of strings)
                pattern = re.compile(r'^([a-zA-Z0-9_\-\.]+)(?:\s*(?:==|>=|<=|~=|>|<)\s*([0-9a-zA-Z\.\-\+]+))?', re.IGNORECASE)
                for dep in deps:
                    match = pattern.match(dep)
                    if match:
                        name = match.group(1)
                        version = match.group(2) or "unknown"
                        packages.append({"name": name, "version": version})
                
                # Parse poetry dependencies (dict)
                for name, version in poetry_deps.items():
                    if name.lower() == "python": 
                        continue
                    if isinstance(version, dict):
                        version = version.get("version", "unknown")
                    packages.append({"name": name, "version": str(version).strip('^~')})
            except Exception as e:
                logger.warning(f"Failed to parse TOML {filepath}: {e}")
        else:
            logger.warning(f"tomllib not available, skipping precise pyproject.toml parsing for {filepath}")
        return packages

    def _parse_package_json(self, filepath: Path) -> list[dict]:
        packages = []
        with open(filepath, 'r', encoding='utf-8') as f:
            try:
                data = json.load(f)
                deps = data.get("dependencies", {})
                dev_deps = data.get("devDependencies", {})
                
                for name, version in deps.items():
                    packages.append({"name": name, "version": version})
                for name, version in dev_deps.items():
                    packages.append({"name": name, "version": version})
            except json.JSONDecodeError:
                logger.warning(f"Malformed JSON in {filepath}")
        return packages

    def _parse_go_mod(self, filepath: Path) -> list[dict]:
        packages = []
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Matches `require github.com/gin-gonic/gin v1.7.4`
        # or inside a require (...) block
        pattern = re.compile(r'(?:require\s+|^|\t)([a-zA-Z0-9\.\-\/\_]+)\s+(v[0-9a-zA-Z\.\-\+]+)', re.MULTILINE)
        for match in pattern.finditer(content):
            packages.append({"name": match.group(1), "version": match.group(2)})
        return packages

    def _parse_cargo_toml(self, filepath: Path) -> list[dict]:
        packages = []
        if tomllib:
            with open(filepath, 'r', encoding='utf-8') as f:
                try:
                    data = tomllib.loads(f.read())
                    deps = data.get("dependencies", {})
                    for name, version in deps.items():
                        if isinstance(version, dict):
                            version = version.get("version", "unknown")
                        packages.append({"name": name, "version": version})
                except Exception as e:
                    logger.warning(f"Error parsing Cargo.toml: {e}")
        return packages

    def _parse_pom_xml(self, filepath: Path) -> list[dict]:
        packages = []
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
            # Handle XML namespace dynamically
            ns_match = re.match(r'\{.*\}', root.tag)
            ns = ns_match.group(0) if ns_match else ''
            
            for dep in root.iter(f'{ns}dependency'):
                group_id = dep.find(f'{ns}groupId')
                artifact_id = dep.find(f'{ns}artifactId')
                version = dep.find(f'{ns}version')
                
                if group_id is not None and artifact_id is not None and version is not None:
                    name = f"{group_id.text}:{artifact_id.text}"
                    packages.append({"name": name, "version": version.text})
        except Exception as e:
            logger.warning(f"Error parsing pom.xml {filepath}: {e}")
        return packages


def main():
    parser = argparse.ArgumentParser(description="DepGuard AI Scanner Agent - Extracts dependencies from config files.")
    parser.add_argument("path", help="Root folder path to scan")
    args = parser.parse_args()

    scanner = ScannerAgent(args.path)
    results = scanner.scan()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    import sys
    
    # Simple logic to either run the CLI or the Unit Tests
    if len(sys.argv) > 1 and sys.argv[1] != "test":
        main()
    else:
        import unittest
        import tempfile
        import shutil

        class TestScannerAgent(unittest.TestCase):
            def setUp(self):
                self.test_dir = tempfile.mkdtemp()
                self.scanner = ScannerAgent(self.test_dir)

            def tearDown(self):
                shutil.rmtree(self.test_dir)

            def test_parse_requirements(self):
                req_path = Path(self.test_dir) / "requirements.txt"
                with open(req_path, "w", encoding="utf-8") as f:
                    f.write("django==3.2.18\nnumpy>=1.21.0\nrequests\n# comment\n")
                
                results = self.scanner.scan()
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["ecosystem"], "pip")
                
                packages = results[0]["packages"]
                self.assertEqual(len(packages), 3)
                self.assertEqual(packages[0], {"name": "django", "version": "3.2.18"})
                self.assertEqual(packages[1], {"name": "numpy", "version": "1.21.0"})
                self.assertEqual(packages[2], {"name": "requests", "version": "unknown"})

            def test_parse_package_json(self):
                pkg_path = Path(self.test_dir) / "package.json"
                with open(pkg_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "dependencies": {"react": "18.2.0"},
                        "devDependencies": {"jest": "^29.0.0"}
                    }, f)
                
                results = self.scanner.scan()
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["ecosystem"], "npm")
                
                packages = results[0]["packages"]
                self.assertEqual(len(packages), 2)
                self.assertEqual(packages[0], {"name": "react", "version": "18.2.0"})
                self.assertEqual(packages[1], {"name": "jest", "version": "^29.0.0"})

            def test_invalid_directory(self):
                invalid_scanner = ScannerAgent("/path/does/not/exist/12345")
                results = invalid_scanner.scan()
                self.assertEqual(results, [])

        # Prevent unittest from seeing the 'test' argument
        sys.argv = [sys.argv[0]]
        print("Running Scanner Agent Unit Tests...\n")
        unittest.main()
