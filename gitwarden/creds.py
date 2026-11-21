"""凭据传递：把凭据放进 git 子进程的环境变量或临时文件，不进入 argv、URL 和日志。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

ASKPASS = Path(__file__).with_name("askpass.sh")
_SCP_RE = re.compile(r"^(?!-)[\w.-]+@(?!-)[\w.-]+:\S+$")


def check_url(url: str) -> None:
    if not isinstance(url, str) or not url:
        raise ValueError(f"不支持的 URL: {url!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in url):
        raise ValueError(f"URL 包含无效的控制或空白字符: {url!r}")
    if _SCP_RE.match(url) or url.startswith("/"):
        return
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http", "ssh"):
        raise ValueError(f"不支持的 URL 协议: {url!r}")
    if parts.password is not None or (scheme != "ssh" and parts.username):
        raise ValueError("URL 不得内嵌凭据，请改用凭据配置")
    if not parts.hostname or parts.hostname.startswith("-"):
        raise ValueError(f"URL 缺少有效主机: {url!r}")
    if parts.username and parts.username.startswith("-"):
        raise ValueError(f"URL 用户名无效: {url!r}")


def check_target_urls(main_url: str, history_url: str) -> None:
    if main_url.strip().rstrip("/") == history_url.strip().rstrip("/"):
        raise ValueError("主镜像与历史仓地址必须不同")
    check_url(main_url)
    check_url(history_url)


def _write_secret_file(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    path.chmod(0o600)


@contextmanager
def git_env(cred: Mapping[str, str | None] | None, workdir: Path) -> Iterator[dict[str, str]]:
    home = workdir / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(home)}
    kind = cred["kind"] if cred else "none"
    if kind == "https":
        env |= {
            "GIT_ASKPASS": str(ASKPASS),
            "GW_USERNAME": cred["username"] or "",
            "GW_SECRET": cred["secret"] or "",
        }
        yield env
    elif kind == "ssh":
        keydir = Path(tempfile.mkdtemp(prefix="ssh-", dir=tempfile.gettempdir()))
        try:
            keydir.chmod(0o700)
            key, kh = keydir / "id", keydir / "known_hosts"
            _write_secret_file(key, cred["private_key"].rstrip("\n") + "\n")
            _write_secret_file(kh, (cred["known_hosts"] or "").rstrip("\n") + "\n")
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes -o BatchMode=yes "
                "-o ServerAliveInterval=30 -o ServerAliveCountMax=10 "
                f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={shlex.quote(str(kh))}"
            )
            yield env
        finally:
            shutil.rmtree(keydir, ignore_errors=True)
    else:
        yield env
