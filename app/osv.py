"""OSV（osv.dev）漏洞查询：依赖 → 已知漏洞，v2 的 CVE 触发数据源。"""

from __future__ import annotations

import asyncio

import httpx

# 依赖清单文件 → OSV 生态系统
DEP_FILES = {
    "requirements.txt": "PyPI",
    "pyproject.toml": "PyPI",
    "package.json": "npm",
    "go.mod": "Go",
    "Cargo.toml": "crates.io",
}

ECOSYSTEM_KEYS = {
    "PyPI": ("dependencies",),
    "npm": ("dependencies", "devDependencies"),
    "Go": ("require",),
    "crates.io": ("dependencies",),
}


class OsvClient:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30)

    async def aclose(self):
        await self._client.aclose()

    async def query(self, eco: str, package: str, version: str) -> list[dict]:
        """查询单个包的已知漏洞。version 未知时留空（返回该包全部漏洞）。"""
        try:
            resp = await self._client.post(
                "https://api.osv.dev/v1/query",
                json={"package": {"name": package, "ecosystem": eco}, "version": version},
            )
        except Exception:  # noqa: BLE001
            return []
        if resp.status_code != 200:
            return []
        vulns = resp.json().get("vulns", [])
        out = []
        for v in vulns[:20]:
            out.append(
                {
                    "id": v.get("id", ""),
                    "summary": (v.get("summary") or v.get("details", ""))[:200],
                    "severity": _severity(v),
                    "fixed": _fixed_versions(v, eco, package),
                    "url": f"https://osv.dev/vulnerability/{v.get('id', '')}",
                }
            )
        return out

    async def query_many(
        self, deps: list[tuple[str, str, str]], concurrency: int = 5
    ) -> dict[tuple[str, str], list[dict]]:
        """批量查询：deps = [(eco, name, version)]；返回 {(eco,name): [vuln]}，只含有漏洞的。"""
        sem = asyncio.Semaphore(concurrency)
        results: dict[tuple[str, str], list[dict]] = {}

        async def one(eco: str, name: str, ver: str):
            async with sem:
                vulns = await self.query(eco, name, ver)
                if vulns:
                    results[(eco, name)] = vulns

        await asyncio.gather(*(one(*d) for d in deps))
        return results


def _severity(vuln: dict) -> str:
    for s in vuln.get("severity", []):
        if s.get("type") == "CVSS_V3":
            score = _cvss_score(s.get("score", ""))
            if score >= 9:
                return "critical"
            if score >= 7:
                return "high"
            if score >= 4:
                return "medium"
            return "low"
    return "unknown"


def _cvss_score(vector: str) -> float:
    try:
        from cvss import CVSS3

        return CVSS3(vector).scores()[0]
    except Exception:  # noqa: BLE001
        return 0.0


def _fixed_versions(vuln: dict, eco: str, package: str) -> list[str]:
    out: list[str] = []
    for aff in vuln.get("affected", []):
        pkg = aff.get("package", {})
        if pkg.get("name") == package and pkg.get("ecosystem") == eco:
            for r in aff.get("ranges", []):
                for ev in r.get("events", []):
                    if "fixed" in ev:
                        out.append(ev["fixed"])
    return out[:5]


def parse_deps(filename: str, content: str) -> list[tuple[str, str, str]]:
    """从依赖清单文件提取 (eco, name, version)；解析失败静默返回空。"""
    eco = DEP_FILES.get(filename.rsplit("/", 1)[-1])
    if not eco:
        return []
    if filename.endswith("requirements.txt"):
        out = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            for sep in ("==", ">=", "~=", "<=", "==="):
                if sep in line:
                    name, _, ver = line.partition(sep)
                    out.append((eco, name.strip().lower(), ver.strip()))
                    break
        return out
    if filename.endswith((".toml", ".json", ".mod")):
        try:
            return _parse_structured(filename, content, eco)
        except Exception:  # noqa: BLE001
            return []
    return []


def _parse_structured(filename: str, content: str, eco: str) -> list[tuple[str, str, str]]:
    import json
    import re

    out: list[tuple[str, str, str]] = []
    if filename.endswith("package.json"):
        data = json.loads(content)
        for key in ECOSYSTEM_KEYS[eco]:
            for name, ver in (data.get(key) or {}).items():
                out.append((eco, name, str(ver).lstrip("^~>=")))
        return out
    if filename.endswith("go.mod"):
        for m in re.finditer(r"^\s+(\S+)\s+v([\d.]+[-\w.]*)", content, re.M):
            path = m.group(1)
            name = path.rsplit("/", 1)[-1] if "/" in path else path
            out.append((eco, path, m.group(2)))
        return out
    # pyproject.toml：只取 PEP 508 依赖名（version 通常未钉死，留空查全量）
    m = re.search(r"(?:dependencies|requires)\s*=\s*\[(.*?)\]", content, re.S)
    if m:
        for dep in re.findall(r'[\"\']([A-Za-z0-9_.-]+)', m.group(1)):
            out.append((eco, dep.lower(), ""))
    return out
