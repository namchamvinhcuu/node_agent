# PCM Node Agent

Chuong trinh chay TREN thiet bi Pi/PC-bridge (`pcm.device.kind = pi | pc |
other`) — doc cam bien (that hoac gia lap) va day len **edge**, dung hop dong
`node_api.py` cua `edge_collector` (`/node/v1/*`).

**Quan trong:** day KHONG phai mot phan cua hop dong `/pcm/api/v1/edge/*` (do
la giua edge <-> Odoo Main, xem `pcm_base/controllers/ingest.py`). Node khong
bao gio noi thang voi Odoo — chi noi voi edge cua no (dung nguyen tac cua PCM:
"아래층이 위층을 부른다": node → edge → Odoo, mot chieu). Hop dong node <-> edge
la phan **edge_collector tu dinh nghia them** vi pcm_base khong quy dinh no.

ESP32 thuc te se chay firmware C/MicroPython rieng, khong dung duoc file
Python nay — coi day la tai lieu tham chieu ve GIAO THUC (`node_api.py`) cho
nguoi viet firmware, va la chuong trinh chay duoc that su cho Pi/PC bridge.

## Chay

```bash
pip install -r requirements.txt
cp .env.example .env                 # sua NODE_EDGE_URL, NODE_SERIAL...
cp channels.example.json channels.json  # sua theo cam bien that cua node nay
python -m node_agent
```

**Venv tao tren mot may KHONG dung lai duoc tren may khac** (duong dan
binary tuyet doi khac nhau giua Windows/Linux). Doi may thi tao venv moi,
dung ghi de venv cu:

```bash
python3 -m venv venv_linux
./venv_linux/bin/pip install -r requirements.txt
./venv_linux/bin/python -m node_agent
```

### Test nhanh khong can thiet bi that

`channels.json` dat toan bo kenh `"mode": "sim"` (xem mau
`channels.example.json`) — node se tu sinh song gia (khong can cam bien/
cong serial that), phu hop test duong ong tu dau (node → edge → Odoo). Cong
serial that (`mode: "serial"`) khong ton tai tren may dev/Linux (vd `COM23`
la cong Windows) se khien reader do lien tuc bao loi khong crash, nhung
KHONG co du lieu — nho doi/them kenh `sim` khi test tren may khac may that.

`NODE_EDGE_URL` phai khop dung dia chi + port ma `edge_collector` dang
lang nghe (mac dinh `8000`, co the da doi neu port do bi chiem tren may
dev — xem `../edge_collector/README.md` muc "Test tren 1 may").

## Setup UI (web) — sua .env/channels.json khong can SSH

Chay kem 5 loop nen (thread rieng, `uvicorn`), mac dinh cong `NODE_SETUP_PORT`
(8091):

```
http://<dia-chi-node>:8091/setup            # sua NODE_EDGE_URL/SERIAL/NAME/KIND/interval
http://<dia-chi-node>:8091/setup/channels   # them/xoa kenh cam bien (sim/serial)
```

Sau khi bam Save, node **tu thoat sach** (`os._exit`) - dua vao `restart:
always` cua Docker de khoi dong lai voi config moi (node_agent khong ho tro
hot-reload reader giua chung). Chay ngoai Docker (vd `python -m node_agent`
truc tiep) se KHONG tu restart - phai tu chay lai tay.

`NODE_SETUP_TOKEN` (mac dinh rong = khong gate gi, dung cho LAN noi bo) - dat
1 gia tri de yeu cau HTTP Basic Auth (username bat ky, password = token nay)
cho toan bo `/setup`.

`docker-compose.hardware.yml` bind-mount ca thu muc `/dev:/dev` (khong liet
ke tung `/dev/ttyUSB0`) - cam cam bien vao cong USB nao cung duoc, chi can
chon dung `port` trong `channels.json` qua web UI, khong can sua file
compose nua.

## `channels.json` — kenh cua CHINH node nay

```json
[
  {"code": "scale_wt", "mode": "sim", "poll_ms": 500, "center": 412.4, "spread": 0.3},
  {"code": "scale_real", "mode": "serial", "port": "/dev/ttyUSB0", "baud": 9600,
   "terminator": "\r\n", "pattern": "^(ST|US),\\s*(GS|NT),\\s*([+-]?[\\d.]+)\\s*(\\w+)$",
   "value_group": 3, "stable_group": 1, "stable_ok": "ST",
   "cmd_zero": "Z\r\n", "cmd_tare": "T\r\n"}
]
```

- `mode: "sim"` — song gia (walk quanh `center` ± `spread`), dung khi chua co
  cam bien that hoac dang thu duong ong.
- `mode: "serial"` — mot cong USB-serial ASCII (`readers/serial_ascii.py`),
  cung cu phap regex/terminator nhu `pcm.serial.profile` ben Odoo nhung khai
  bao CUC BO (node khong tu dong keo profile tu Odoo trong MVP nay).
- `mode: "modbus"` — Modbus RTU (`conn_type: "rtu"`, qua `port`/`baud`) hoac
  TCP (`conn_type: "tcp"`, qua `host`/`tcp_port`) — dung cho thiet bi cong
  nghiep (bien tan, PLC, cam bien Modbus). `register_type` (holding/input),
  `unit_id` (slave address), `address` (offset thanh ghi), `data_type`
  (u16/i16/u32/i32/f32 - quyet dinh cach ghep byte tu 1-2 thanh ghi 16-bit),
  `scale`/`offset` (gia tri that = raw*scale + offset). Ho tro `command("write",
  value)` de ghi nguoc xuong holding register (vd dieu khien relay/setpoint
  qua bien tan) - input register la read-only theo dung chuan Modbus. Tu
  retry ket noi voi backoff (1s->30s) khi mat mang, KHONG die vinh vien nhu
  `serial` (thiet bi Modbus mang thuong gian doan tam thoi hon la loi vinh
  vien). **Chua test voi thiet bi/PLC Modbus that** (khong co trong moi
  truong phat trien) - chi verify bang mock + test ket noi that toi dia chi
  khong ton tai (xac nhan tu retry dung, khong crash).
- `mode: "mqtt"` — subscribe DUNG 1 `topic` tren broker (`host`/`port`,
  `username`/`password` optional). Khac Modbus/serial (node CHU DONG poll),
  MQTT la PUSH-based: broker gui gia tri moi bat cu luc nao qua callback
  `on_message` cua `paho-mqtt`, khong co `poll_ms`. Payload mac dinh la so
  tho (`float(payload)`); neu thiet bi gui JSON, khai bao `json_key` de lay
  dung 1 key phang (vd `{"v": 23.4}` + `json_key: "v"`) - KHONG ho tro nested
  path. Ho tro `command()` bang cach publish xuong `cmd_topic` (mac dinh
  `<topic>/cmd`). Thu vien `paho-mqtt` tu dong reconnect voi backoff khi mat
  ket noi broker - khong can tu viet retry loop nhu Modbus. **Chua test voi
  broker MQTT that** (khong co trong moi truong phat trien) - chi verify
  bang mock callback `on_connect`/`on_message`/`on_disconnect` thu cong.

Them mode moi (I2C/GPIO cam thang vao node, MQTT, OPC-UA...) bang cach viet
mot class ke thua `readers.base.ChannelReader` (`_run()` + `command()`), dang
ky vao `READER_CLASSES` trong `agent.py`. Xem `.obsidian-vault/Plan/
node-agent-multi-protocol-readers.md` cho ke hoach cac mode dang lam.

## Giao thuc `/node/v1/*` (tren edge_collector)

| Duong | Chieu | Noi dung |
|---|---|---|
| `POST /node/v1/hello` | node → edge | Bao ten/loai; edge tra `api_key` NEU Odoo da tao `pcm.device` cho serial nay (edge da dong bo qua `edge/config`) |
| `POST /node/v1/measurements` | node → edge | `{items:[{ch,v,s,q,ts,stable}], bid, seq}` — dedup theo `(bid,seq)` luu SQLite, song sot qua restart |
| `POST /node/v1/heartbeat` | node → edge | Duoc CHUYEN TIEP NGAY sang `/pcm/api/v1/heartbeat` cua Odoo |
| `GET /node/v1/commands` | node poll | Lenh dang cho (zero/tare/...) — edge xep hang khi Odoo goi `/api/command` cho serial nay ma khong co driver nao dieu khien duoc (kind=http_node) |
| `POST /node/v1/commands/ack` | node → edge | Ket qua lenh — danh thuc request `/api/command` dang cho ben Odoo (timeout 8s neu node im lang) |

Xac thuc: header `X-Device-Serial` luon can; `X-API-Key` BAT BUOC neu edge da
biet khoa cho serial do (Odoo da tao thiet bi), CHO QUA neu chua (lan dau
tiep xuc — giong cach `pcm.edge._hello()` doi xu voi mot `edge_code` la).

## Da kiem chung (khong chi doc code)

Chay `edge_collector` that (uvicorn, socket that) + `node_agent` that (dung
`requests`, khong mock) trong cung mot may: node day gia tri sim → thay ngay
qua `/api/latest` cua edge; gia lap Odoo goi `/api/command` → node poll lay
lenh → ack → request cho ben Odoo nhan duoc ket qua dung.

**Da verify THAT voi Main (Odoo) that** (khong con la gia lap): chay them
`pcmppdpod_dev` local cung may, node tu dang ky (`serial` moi) → edge
chuyen tiep len Odoo → `pcm.device`/`pcm.channel` duoc Odoo tu tao va nhan
gia tri realtime. Truoc do co gap 1 bug That o phia `pcm_base` (route
`auth='none'` crash HTTP 500 khi edge/device MOI tu dang ky lan dau) — da
fix + test hoi quy + review, xem
`pcmppdpod_odoo/.obsidian-vault/Fix-History/2026-09-16-ingest-auth-none-message-post-expected-singleton.md`.
Khi Main tam ngung/khong ket noi duoc, moi vong lap cua ca edge lan node tu
bat loi, khong crash, tu thu lai (xac nhan hanh vi nay dung nhu thiet ke).

**Chua test**: `mode: "serial"` voi cam bien phan cung that (khong co thiet
bi trong moi truong phat trien).
