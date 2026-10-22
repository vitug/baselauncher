# -*- coding: utf-8 -*-
"""stdio-мост к MCP-серверу BaseLauncher.

MCP — часть приложения: инструменты живут в самом BaseLauncher на
`http://127.0.0.1:8797/mcp` (POST JSON-RPC 2.0). Этот скрипт нужен только тем клиентам,
которые умеют исключительно stdio: он читает JSON-RPC из stdin, отправляет в приложение и
печатает ответ в stdout. Ничего своего не решает — вся логика в `app/mcp.py`.

Подключение в клиенте:
    command: <python>   args: [mcp_server.py]
или сразу HTTP-транспортом:  url: http://127.0.0.1:8797/mcp   (предпочтительно)

Переменные: BL_PORT (8797), BL_URL (полный адрес, перекрывает порт).
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get('BL_URL') or f"http://127.0.0.1:{os.environ.get('BL_PORT', '8797')}/mcp"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # без WinINET-прокси


def forward(msg):
    data = json.dumps(msg, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(URL, data=data,
                                 headers={'Content-Type': 'application/json',
                                          'Accept': 'application/json'})
    try:
        raw = OPENER.open(req, timeout=600).read().decode('utf-8')
        return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        try:
            return json.loads(body)
        except ValueError:
            return {'jsonrpc': '2.0', 'id': msg.get('id'),
                    'error': {'code': -32603, 'message': f'HTTP {e.code}: {body[:400]}'}}
    except Exception as e:                                            # noqa: BLE001
        rid = msg.get('id')
        return ({'jsonrpc': '2.0', 'id': rid,
                 'error': {'code': -32000, 'message': f'BaseLauncher недоступен: {e} — '
                                                      f'запустите приложение (start_bl.bat)'}}
                if rid is not None else None)


def main() -> int:
    # построчный протокол: одно JSON-RPC сообщение на строку
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {'jsonrpc': '2.0', 'id': None,
                   'error': {'code': -32700, 'message': 'Parse error'}}
        else:
            out = forward(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + '\n')
            sys.stdout.flush()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
