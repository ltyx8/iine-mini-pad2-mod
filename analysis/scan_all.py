import asyncio
from bleak import BleakScanner

async def main():
    print("扫描 12 秒（全部 BLE 设备）…", flush=True)
    found = {}
    def cb(dev, adv):
        rssi = getattr(adv, "rssi", None)
        found[dev.address] = (dev.name, rssi, [str(u) for u in (adv.service_uuids or [])])
    async with BleakScanner(detection_callback=cb):
        await asyncio.sleep(12.0)
    print(f"共 {len(found)} 个", flush=True)
    for addr, (name, rssi, uuids) in found.items():
        print(f"  {addr}  rssi={rssi}  name={name!r}  uuids={uuids[:3]}", flush=True)

asyncio.run(main())
