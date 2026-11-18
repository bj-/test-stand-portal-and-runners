"""Ограниченное ожидание HTTP health локальной установки; только stdlib."""
import argparse
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
from portal_local import LocalError, load_workspace, workspace_path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--timeout", type=int, default=180, choices=range(1, 601), metavar="1..600")
    args = parser.parse_args(argv)
    try:
        data = load_workspace(workspace_path(args.workspace))
    except (LocalError, OSError, UnicodeError):
        print("Не удалось прочитать marker установки для проверки health.", file=sys.stderr)
        return 1
    base_url = f"http://127.0.0.1:{data['port']}"
    url = base_url + "/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + args.timeout
    print(f"Ожидаю {url} (до {args.timeout} секунд)…", flush=True)
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            with opener.open(url, timeout=min(3, remaining)) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    print(f"Портал доступен: {base_url}")
                    return 0
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    print("Health пока недоступен. Данные сохранены; повторите installer или проверьте launcher logs.", file=sys.stderr)
    return 20


if __name__ == "__main__":
    raise SystemExit(main())
