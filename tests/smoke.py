"""One real inference request to a running proxy; uses your Copilot quota."""
import argparse
import json
import sys
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=8787)
parser.add_argument("--model", default="claude-sonnet-4.6")
args = parser.parse_args()
request = urllib.request.Request(
    "http://127.0.0.1:%d/v1/messages" % args.port,
    data=json.dumps({"model": args.model, "max_tokens": 64,
                     "messages": [{"role": "user", "content": "Reply with hello."}]}).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        print("HTTP", response.status)
        print(response.read().decode())
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode(), file=sys.stderr)
    sys.exit(1)
except OSError as e:
    print("Could not reach proxy:", e, file=sys.stderr)
    sys.exit(1)
