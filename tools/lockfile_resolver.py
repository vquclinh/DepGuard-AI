import json
import logging
import re
from pathlib import Path
from typing import Optional
import tomllib

logger = logging.getLogger(__name__)

# Find version of dependencies in lockfile
class LockfileResolver:
    def resolve(self, package_name: str, project_root: str) -> Optional[str]:
        root = Path(project_root)
        name_lower = package_name.lower()

        resolvers = [
            ("Pipfile.lock",        self._resolve_pipfile_lock),
            ("poetry.lock",         self._resolve_poetry_lock),
            ("package-lock.json",   self._resolve_package_lock_json),
            ("yarn.lock",           self._resolve_yarn_lock),
            ("Cargo.lock",          self._resolve_cargo_lock),
            ("go.sum",              self._resolve_go_sum),
        ]

        for filename, resolver_fn in resolvers:
            lockfile = root / filename
            if lockfile.exists() and lockfile.is_file():
                try:
                    version = resolver_fn(lockfile, name_lower)
                    if version:
                        logger.debug(f"Resolved {package_name}=={version} from {filename}")
                        return version
                except Exception as e:
                    logger.debug(f"Error reading {filename} for {package_name}: {e}")

        return None

    # Pipfile.lock
    def _resolve_pipfile_lock(self, lockfile: Path, name_lower: str) -> Optional[str]:
        with open(lockfile, "r", encoding="utf-8") as f:
            data = json.load(f)

        for section in ("default", "develop"):
            section_data = data.get(section, {})
            for pkg_name, pkg_info in section_data.items():
                if pkg_name.lower() == name_lower:
                    version = pkg_info.get("version", "")
                    # Pipfile.lock stores versions as "==3.2.18"
                    return version.lstrip("=") if version else None

        return None

    # poetry.lock
    def _resolve_poetry_lock(self, lockfile: Path, name_lower: str) -> Optional[str]:
        if tomllib is None:
            logger.warning("tomllib/tomli not available; cannot parse poetry.lock")
            return None

        with open(lockfile, "rb") as f:
            data = tomllib.load(f)

        for pkg in data.get("package", []):
            if pkg.get("name", "").lower() == name_lower:
                return pkg.get("version")

        return None

    # package-lock.json
    def _resolve_package_lock_json(self, lockfile: Path, name_lower: str) -> Optional[str]:
        with open(lockfile, "r", encoding="utf-8") as f:
            data = json.load(f)

        # npm v2/v3 lockfile: "packages" key uses paths like "node_modules/react"
        packages = data.get("packages", {})
        for path, info in packages.items():
            pkg_name = path.replace("node_modules/", "").lower()
            if pkg_name == name_lower:
                return info.get("version")

        # npm v1 lockfile: "dependencies" key with flat names
        dependencies = data.get("dependencies", {})
        for pkg_name, info in dependencies.items():
            if pkg_name.lower() == name_lower:
                return info.get("version")

        return None

    # yarn.lock
    def _resolve_yarn_lock(self, lockfile: Path, name_lower: str) -> Optional[str]:
        content = lockfile.read_text(encoding="utf-8")

        current_match = False
        for line in content.splitlines():
            stripped = line.strip()
            if not line.startswith(" ") and stripped.endswith(":") and "@" in stripped:
                specifiers = stripped.rstrip(":").replace('"', "").split(", ")
                current_match = any(
                    spec.rsplit("@", 1)[0].lower() == name_lower
                    for spec in specifiers
                    if "@" in spec
                )
            elif current_match and stripped.startswith("version"):
                version_match = re.search(r'version\s+"([^"]+)"', stripped)
                if version_match:
                    return version_match.group(1)

        return None

    # Cargo.lock
    def _resolve_cargo_lock(self, lockfile: Path, name_lower: str) -> Optional[str]:
        if tomllib is None:
            logger.warning("tomllib/tomli not available; cannot parse Cargo.lock")
            return None

        with open(lockfile, "rb") as f:
            data = tomllib.load(f)

        for pkg in data.get("package", []):
            if pkg.get("name", "").lower() == name_lower:
                return pkg.get("version")

        return None

    # go.sum
    def _resolve_go_sum(self, lockfile: Path, name_lower: str) -> Optional[str]:
        pattern = re.compile(r'^(\S+)\s+(v[0-9a-zA-Z.\-+]+)\s+h1:', re.MULTILINE)
        content = lockfile.read_text(encoding="utf-8")

        for match in pattern.finditer(content):
            module = match.group(1).lower()
            if module == name_lower or module.split("/")[-1] == name_lower:
                return match.group(2)

        return None
