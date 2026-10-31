// JoyMiLink.cs — 良值/iINE 手柄 JoyMi 私有 BLE 协议 · Windows 端骨架
//
// 目标框架：net8.0-windows10.0.19041.0（WinRT GATT 需要 Windows 10 1809+）
// NuGet   ：<PackageReference Include="MessagePack" Version="2.5.*" />
//
// 工程文件要点（.csproj）：
//   <TargetFramework>net8.0-windows10.0.19041.0</TargetFramework>
//   <SupportedOSPlatformVersion>10.0.17763.0</SupportedOSPlatformVersion>
//   <UseWinUI>true</UseWinUI>   <!-- 用 WinUI3 时；WPF 则 <UseWPF>true</UseWPF> 并引用 Microsoft.Windows.SDK.Contracts -->
//
// 说明：本文件为静态反编译结论的直译骨架，未在本机编译（无 .NET SDK）。
//      协议字段出处见《良值IINE_APK逆向与Windows客户端方案.md》第 2–6 节。

using System;
using System.Buffers;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Threading;
using System.Threading.Tasks;
using MessagePack;
using Windows.Devices.Bluetooth;
using Windows.Devices.Bluetooth.GenericAttributeProfile;
using Windows.Storage.Streams;

namespace Iine.JoyMi;

/// <summary>一条改键规则。字段含义见报告 §4。</summary>
public sealed class RemapKey
{
    public byte Key { get; set; }        // k  : 源键码（§5 键表）
    public byte Type { get; set; }       // ty : 映射类型（枚举值待真机确认）
    public byte Mode { get; set; }       // m  : 子模式
    public byte Parameter { get; set; }  // p  : 参数字节（常见为目标键码）
    public ushort Repeat { get; set; }   // s  : 连发次数/秒，1000 / turboIntervalMs
    public List<byte> Targets { get; } = new(); // rk : 多重映射目标键
}

/// <summary>g_kt 上报的一帧按键状态。</summary>
public readonly record struct KeyTestState(
    IReadOnlyList<int> PressedKeys,
    int LeftX, int LeftY, int RightX, int RightY,
    int LeftTrigger, int RightTrigger);

public static class JoyMiFrame
{
    public const byte Head = 0xB4;
    public const byte Tag = 0xCC;

    /// <summary>把一条 msgpack payload 切成多帧；分片大小 = mtu - 7。</summary>
    public static List<byte[]> Build(ReadOnlySpan<byte> payload, int mtu = 247)
    {
        int chunk = Math.Max(16, mtu - 7);
        int count = (payload.Length + chunk - 1) / chunk;
        if (count is 0) return new List<byte[]>();
        if (count > 127) throw new InvalidOperationException("片数超过 127");

        var frames = new List<byte[]>(count);
        for (int i = 0; i < count; i++)
        {
            int offset = i * chunk;
            int len = Math.Min(chunk, payload.Length - offset);
            int seq = count - i;                 // 首片 = 总片数，最后一片 = 1
            if (i == 0) seq |= 0x80;             // bit7 标记新消息首片

            var f = new byte[len + 4];
            f[0] = Head;
            f[1] = (byte)(len + 4);              // 本帧总长
            f[2] = (byte)seq;
            f[3] = Tag;
            payload.Slice(offset, len).CopyTo(f.AsSpan(4));
            frames.Add(f);
        }
        return frames;
    }
}

/// <summary>接收端重组：一条 msgpack 消息可能跨多个通知。</summary>
public sealed class JoyMiAssembler
{
    private readonly List<byte> _buf = new(512);

    /// <summary>喂入一帧；仅在最后一片(seq==1)时返回完整 payload。</summary>
    public byte[]? Feed(ReadOnlySpan<byte> frame)
    {
        if (frame.Length < 4 || frame[0] != JoyMiFrame.Head) return null;
        if (frame[1] != frame.Length) return null;          // LEN 校验
        if (frame[3] != JoyMiFrame.Tag) return null;

        int seq = frame[2];
        bool first = (seq & 0x80) != 0;
        int remain = seq & 0x7F;

        if (first) _buf.Clear();
        _buf.AddRange(frame[4..].ToArray());

        if (remain != 1) return null;
        var payload = _buf.ToArray();
        _buf.Clear();
        return payload;
    }
}

public sealed class JoyMiLink : IAsyncDisposable
{
    public static readonly Guid ServiceUuid = new("0000AE40-0000-1000-8000-00805F9B34FB");
    public static readonly Guid RxCharUuid = new("0000AE41-0000-1000-8000-00805F9B34FB"); // 写
    public static readonly Guid TxCharUuid = new("0000AE42-0000-1000-8000-00805F9B34FB"); // notify

    private readonly JoyMiAssembler _asm = new();
    private readonly Dictionary<string, TaskCompletionSource<byte[]>> _pending = new();
    private readonly SemaphoreSlim _writeLock = new(1, 1);

    private BluetoothLEDevice? _device;
    private GattSession? _session;
    private GattCharacteristic? _rx;

    public int Mtu { get; private set; } = 247;
    public string? DeviceName { get; private set; }

    /// <summary>按键状态实时流（g_kt），已做去重前处理。</summary>
    public event Action<KeyTestState>? KeyTestReceived;
    public event Action<bool>? ConnectionChanged;

    /// <summary>扫描未配对设备，按名称关键字过滤（IINE_ 前缀 / Wireless Controller）。</summary>
    public static async Task<IReadOnlyList<BluetoothLEDevice>> ScanAsync(TimeSpan timeout)
    {
        var hits = new List<BluetoothLEDevice>();
        var watcher = DeviceInformation.CreateWatcher(BluetoothLEDevice.GetDeviceSelector());

        async void OnAdded(DeviceWatcher sender, DeviceInformation info)
        {
            var n = info.Name ?? "";
            if (!n.Contains("IINE", StringComparison.OrdinalIgnoreCase) &&
                !n.Contains("Wireless Controller", StringComparison.OrdinalIgnoreCase))
                return;
            var d = await BluetoothLEDevice.FromIdAsync(info.Id);
            if (d is not null) lock (hits) { hits.Add(d); }
        }

        watcher.Added += OnAdded;
        watcher.Start();
        await Task.Delay(timeout);
        watcher.Stop();
        watcher.Added -= OnAdded;
        return hits;
    }

    public async Task ConnectAsync(BluetoothLEDevice device)
    {
        _device = device;
        DeviceName = device.Name;

        var svcResult = await device.GetGattServicesForUuidAsync(ServiceUuid, BluetoothCacheMode.Uncached);
        if (svcResult.Status != GattCommunicationStatus.Success || svcResult.Services.Count == 0)
            throw new InvalidOperationException($"未找到 AE40 服务: {svcResult.Status}");

        var service = svcResult.Services[0];

        // 关键：保持连接，并在可能时拿到协商后的 PDU 大小
        _session = await GattSession.FromDeviceIdAsync(device.BluetoothDeviceId);
        if (_session is not null)
        {
            _session.MaintainConnection = true;
            _session.MaxPduSizeChanged += (_, _) =>
                Mtu = _session!.MaxPduSize > 0 ? _session!.MaxPduSize : 247;
            Mtu = _session.MaxPduSize > 0 ? _session.MaxPduSize : 247;
        }

        var rxResult = await service.GetCharacteristicsForUuidAsync(RxCharUuid, BluetoothCacheMode.Uncached);
        var txResult = await service.GetCharacteristicsForUuidAsync(TxCharUuid, BluetoothCacheMode.Uncached);
        _rx = rxResult.Characteristics.FirstOrDefault()
              ?? throw new InvalidOperationException("未找到 AE41");
        var tx = txResult.Characteristics.FirstOrDefault()
              ?? throw new InvalidOperationException("未找到 AE42");

        var notify = await tx.WriteClientCharacteristicConfigurationDescriptorWithResultAsync(
            GattClientCharacteristicConfigurationDescriptorValue.Notify);
        if (notify.Status != GattCommunicationStatus.Success)
            throw new InvalidOperationException($"订阅失败: {notify.Status}");

        tx.ValueChanged += OnValueChanged;
        ConnectionChanged?.Invoke(true);
    }

    private void OnValueChanged(GattCharacteristic sender, GattValueChangedEventArgs args)
    {
        var frame = new byte[args.CharacteristicValue.Length];
        using (var dr = DataReader.FromBuffer(args.CharacteristicValue))
            dr.ReadBytes(frame);

        var payload = _asm.Feed(frame);
        if (payload is null) return;

        // 先按 cmd 分发：g_kt 是设备主动推的流，不参与请求-响应配对
        try
        {
            var reader = new MessagePackReader(payload);
            var mapLen = reader.ReadMapHeader();
            string? cmd = null;
            for (int i = 0; i < mapLen; i++)
            {
                var k = reader.ReadString();
                if (k == "cmd") cmd = reader.ReadString();
                else reader.Skip();
            }

            if (cmd == "g_kt")
            {
                var st = ParseKeyTest(payload);
                if (st is not null) KeyTestReceived?.Invoke(st.Value);
                return;
            }

            if (cmd is not null && _pending.TryGetValue(cmd, out var tcs))
            {
                _pending.Remove(cmd);
                tcs.TrySetResult(payload);
            }
        }
        catch
        {
            // 单帧解析失败不应断开连接
        }
    }

    /// <summary>g_kt 解析：map 列表首元素 = {key[], lx, ly, rx, ry|r_y, l2, r2}</summary>
    public static KeyTestState? ParseKeyTest(ReadOnlyMemory<byte> payload)
    {
        var r = new MessagePackReader(payload);
        int n = r.ReadMapHeader();
        MessagePackReader? inner = null;
        for (int i = 0; i < n; i++)
        {
            if (r.ReadString() == "map") inner = r.CreateReader();
            else r.Skip();
        }
        if (inner is null) return null;

        var mr = inner.Value;
        if (mr.NextMessagePackType == MessagePackType.Array)
        {
            if (mr.ReadArrayHeader() == 0) return null;
        }
        if (mr.NextMessagePackType != MessagePackType.Map) return null;

        var keys = new List<int>();
        int lx = 0, ly = 0, rx = 0, ry = 0, r_y = int.MinValue, l2 = 0, r2 = 0;
        int fields = mr.ReadMapHeader();
        for (int i = 0; i < fields; i++)
        {
            switch (mr.ReadString())
            {
                case "key":
                    int c = mr.ReadArrayHeader();
                    for (int j = 0; j < c; j++) keys.Add(mr.ReadInt32());
                    break;
                case "lx": lx = mr.ReadInt32(); break;
                case "ly": ly = mr.ReadInt32(); break;
                case "rx": rx = mr.ReadInt32(); break;
                case "ry": ry = mr.ReadInt32(); break;
                case "r_y": r_y = mr.ReadInt32(); break;
                case "l2": l2 = mr.ReadInt32(); break;
                case "r2": r2 = mr.ReadInt32(); break;
                default: mr.Skip(); break;
            }
        }
        if (ry == 0 && r_y != int.MinValue) ry = r_y;      // 与 App 一致的兜底
        return new KeyTestState(keys, lx, ly, rx, ry, l2, r2);
    }

    // ------------------------------------------------------------ 发送
    public async Task SendAsync(ReadOnlyMemory<byte> payload, CancellationToken ct = default)
    {
        if (_rx is null) throw new InvalidOperationException("未连接");
        await _writeLock.WaitAsync(ct);
        try
        {
            foreach (var frame in JoyMiFrame.Build(payload.Span, Mtu))
            {
                var result = await _rx.WriteValueWithResultAsync(
                    frame.AsBuffer(), GattWriteOption.WriteWithoutResponse);
                if (result.Status != GattCommunicationStatus.Success)
                    throw new InvalidOperationException($"写入失败: {result.Status}");
                await Task.Delay(30, ct);   // 分片节流，Android 端用 ACK，这里用间隔
            }
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public async Task<byte[]?> RequestAsync(string cmd, Action<MessagePackWriter>? buildBody = null,
                                           TimeSpan? timeout = null)
    {
        var buffer = new ArrayBufferWriter<byte>();
        var w = new MessagePackWriter(buffer);
        w.WriteMapHeader(buildBody is null ? 1u : 2u);
        w.Write("cmd"); w.Write(cmd);
        buildBody?.Invoke(w);
        w.Flush();

        var tcs = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[cmd] = tcs;
        await SendAsync(buffer.WrittenMemory);
        var done = await Task.WhenAny(tcs.Task, Task.Delay(timeout ?? TimeSpan.FromSeconds(3)));
        _pending.Remove(cmd);
        return done == tcs.Task ? tcs.Task.Result : null;
    }

    // ------------------------------------------------------------ 业务命令
    public Task<byte[]?> GetDeviceInfoAsync() => RequestAsync("g_devinfo");
    public Task<byte[]?> GetBatteryAsync() => RequestAsync("g_bat");

    /// <summary>注意：读改键档位的 map 是「十进制字符串」，写接口是 List&lt;Map&gt;，这是 App 的既有差异。</summary>
    public Task<byte[]?> GetRemapAsync(byte mode) => RequestAsync("g_rkey", w =>
    {
        w.Write("map");
        w.Write(mode.ToString());       // 字符串！
    });

    public Task<byte[]?> SetRemapAsync(byte mode, IReadOnlyList<RemapKey> rules) => RequestAsync("s_rkey", w =>
    {
        w.Write("map");
        w.WriteArrayHeader(1);
        w.WriteMapHeader(2);
        w.Write("mod"); w.WriteUInt8(mode);     // 显式 uint8 = 0xCC
        w.Write("ks");
        w.WriteArrayHeader(rules.Count);
        foreach (var r in rules)
        {
            w.WriteMapHeader(6);
            w.Write("k");  w.WriteUInt8(r.Key);
            w.Write("ty"); w.WriteUInt8(r.Type);
            w.Write("m");  w.WriteUInt8(r.Mode);
            w.Write("p");  w.WriteUInt8(r.Parameter);
            w.Write("s");  w.WriteUInt16(r.Repeat);   // 显式 uint16 = 0xCD
            w.Write("rk");
            w.WriteArrayHeader(r.Targets.Count);
            foreach (var t in r.Targets) w.WriteUInt8(t);
        }
    });

    /// <summary>开/关实时按键上报。用完务必关闭，否则手柄持续高频广播。</summary>
    public Task<byte[]?> SetKeyTestAsync(bool on) => RequestAsync("s_key_test", w =>
    {
        w.Write("map");
        w.WriteArrayHeader(1);
        w.WriteMapHeader(1);
        w.Write("sw"); w.WriteUInt8((byte)(on ? 1 : 0));
    });

    public async ValueTask DisposeAsync()
    {
        try
        {
            if (_device is not null) await SetKeyTestAsync(false);
        }
        catch { /* 忽略 */ }
        if (_session is not null) { _session.MaintainConnection = false; _session.Dispose(); }
        _device?.Dispose();
        ConnectionChanged?.Invoke(false);
    }
}

/// <summary>键码表（报告 §5），用于 UI 显示与下拉框。</summary>
public static class GamepadKeys
{
    public static readonly IReadOnlyDictionary<int, string> Names = new Dictionary<int, string>
    {
        [1]="A",[2]="B",[3]="X",[4]="Y",[5]="L1",[6]="R1",[7]="L2",[8]="R2",
        [9]="Select",[10]="Start",[11]="L3",[12]="R3",[13]="Down",[14]="Up",
        [15]="Right",[16]="Left",[17]="Home",[18]="L3D",[19]="R3D",
        [20]="L4",[21]="R4",[22]="L5",[23]="R5",
        [92]="Sensor",[93]="Minus",[94]="Plus",[95]="CAP",[96]="Turbo",
        [97]="ML",[98]="MR",[99]="Mode",[100]="Fn",[101]="SL",[102]="SR",
        [103]="M1",[104]="M2",[105]="M3",[106]="M4",[107]="M5",[108]="M6",
    };
}
