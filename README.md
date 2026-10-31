# 良值复古迷你手柄 2（IINE Mini Retro Ananke Gen 2）· Windows 改键工具

在 PC 上改手柄键位：本地起一个桥，浏览器里点按键就能改，不用装手机 App。

- 手柄：良值（IINE）复古迷你手柄 2 · `VID 1000 / PID 2001` · `joymi` 平台
- 通道：BLE 服务 `0xAE40`（写 `0xAE41`，通知 `0xAE42`），msgpack 报文

## 运行

需要 Windows 10 及以上、Python 3.10+。

```bash
pip install -r requirements.txt
python analysis/bridge.py --port 8848 --open
```

浏览器会自动打开 `http://127.0.0.1:8848/keymap.html`，点「连接手柄」。

参数：`--addr <MAC>` 指定地址 · `--no-auto` 启动时不自动连 · `--chunk N` 改分片大小 · `--no-open` 不自动开浏览器

## 手柄档位（最影响成败）

手柄拨到不同档位，连 Windows 的方式完全不同：

| 档位 | Windows 侧连接方式 | 实测结果 |
|---|---|---|
| **鼠标档** | BLE HID（`BTHLE`，链路现成） | **最推荐**，1.5 秒连上 |
| **键盘档** | 经典蓝牙 HID | 首连约 8 秒，重连约 1 秒 |
| 私有 / 配置档 | 不连（无驱动） | 专为配置设计，稳定 |
| Xbox 档 | 经典蓝牙 HID | 重启后要等链路稳定，可能 2~3 分钟 |
| Switch 档 | 经典蓝牙（`Pro Controller`） | 最难，只能抢经典连接断开的瞬间 |

改键用鼠标档或私有档，改完拨回去打游戏，配置存在固件里不会丢。

手柄每切一次档，BLE 地址都会变（随机地址），程序按「缓存地址 → 已配对列表 → 扫描」逐级找回。

## 界面

- **手柄** —— 键映射。有「改键方式」开关：`单向赋值`（只改这一个键）/ `交换两键`（互相调换）
- **键盘** —— 最多 2 个普通键 + 2 个修饰键，所以能做 `Ctrl+C` 这类组合
- **鼠标** —— 左右键、滚轮、移动方向与步长（1–48）
- **媒体键** —— 音量、播放暂停、上下曲、亮度、静音
- **其他** —— 无操作自动关机时间

改完点「应用」写入固件，程序自动回读校验。

## 目录结构

```
analysis/
├── bridge.py            本地桥：HTTP 服务 + BLE GATT 双向通道（核心）
├── winrt_link.py        WinRT 链路与 V1 帧构建
├── web/keymap.html      可视化改键页面（主界面）
├── web/assets/          手柄底图与按键图标
├── web/data/            键位坐标表、机型表
├── kb_probe.py          键盘表读写（set_kb）
├── mouse_probe.py       鼠标表读写（set_mouse）
├── cs_probe.py          媒体键表读写（set_cs）
├── gp_probe.py          手柄映射表读写（set_gp）
├── *_backup.json        各表出厂基线备份
└── V1协议实测与改键说明.md
```

## 协议要点

帧格式：`B4 | LEN | SEQ | CC | payload`。`LEN` 为整帧长度；`SEQ` 高位 `0x80` 表示消息首帧、低位 `0x7F` 为 1 表示末帧；分片净载荷 = `MTU - 7`（协商到 64，即每片 57 字节）。

| 表 | 命令 | 结构 |
|---|---|---|
| 键盘 | `get_kb` / `set_kb` | `{key: u8, fn: u8, kb: [键1, 键2, 槽位号]}` |
| 手柄 | `get_gp` / `set_gp` | `{key: u8, gp: u8}`，`gp` 是目标槽位 |
| 鼠标 | `get_mouse` / `set_mouse` | `{key: u8, but: u8, w: i16, x: i16, y: i16}` |
| 媒体键 | `get_cs` / `set_cs` | `{key: u16, cs: u16}`，Consumer Page usage |
| 关机 | `get_sleep` / `set_sleep` | `{discon: u8, con: u8}`，分钟，0 = 不关 |

写表一律**全量**：条目数要与读到的条数一致，只发一条固件会忽略。`set_*` 没有 ACK，是否写入成功只能靠回读判断。

槽位号：`0=A 1=B 3=X 4=Y 6=L 7=R 8=ZL 9=ZR 10=− 11=+ 12=截图 15=HOME 16=↑ 17=↓ 18=← 19=→ 23=M1`

## 许可

[MIT](LICENSE)。

## 免责

仅用于自有设备的键位配置。协议与报文来自对官方 App 的抓包与静态分析，未修改固件、不涉及绕过任何授权。
