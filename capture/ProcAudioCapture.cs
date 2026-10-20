// Captura o audio de um processo (e seus filhos) via WASAPI Process Loopback
// (Windows 10 2004+). Escreve PCM 16-bit, 16 kHz, mono no stdout.
// Uso: ProcAudioCapture.exe <PID>
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

static class Native
{
    public const int VT_BLOB = 65;
    public const int AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1;
    public const int PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0;
    public const uint AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000;
    public const uint AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000;
    public const uint AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000;
    public const uint AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000;
    public const uint AUDCLNT_BUFFERFLAGS_SILENT = 0x2;

    [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = false)]
    public static extern void ActivateAudioInterfaceAsync(
        [MarshalAs(UnmanagedType.LPWStr)] string deviceInterfacePath,
        [MarshalAs(UnmanagedType.LPStruct)] Guid riid,
        IntPtr activationParams,
        IActivateAudioInterfaceCompletionHandler completionHandler,
        out IActivateAudioInterfaceAsyncOperation activationOperation);
}

[StructLayout(LayoutKind.Sequential)]
struct AudioClientActivationParams
{
    public int ActivationType;
    public uint TargetProcessId;
    public int ProcessLoopbackMode;
}

[StructLayout(LayoutKind.Explicit, Size = 24)]
struct PropVariantBlob
{
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public uint cbSize;
    [FieldOffset(16)] public IntPtr pBlobData;
}

[StructLayout(LayoutKind.Sequential, Pack = 2)]
struct WaveFormatEx
{
    public ushort wFormatTag;
    public ushort nChannels;
    public uint nSamplesPerSec;
    public uint nAvgBytesPerSec;
    public ushort nBlockAlign;
    public ushort wBitsPerSample;
    public ushort cbSize;
}

[ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IActivateAudioInterfaceCompletionHandler
{
    void ActivateCompleted(IActivateAudioInterfaceAsyncOperation activateOperation);
}

[ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IActivateAudioInterfaceAsyncOperation
{
    void GetActivateResult(out int activateResult, [MarshalAs(UnmanagedType.IUnknown)] out object activatedInterface);
}

[ComImport, Guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAgileObject { }

[ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioClient
{
    [PreserveSig] int Initialize(int shareMode, uint streamFlags, long hnsBufferDuration, long hnsPeriodicity, [In] ref WaveFormatEx format, IntPtr audioSessionGuid);
    int GetBufferSize(out uint bufferFrames);
    long GetStreamLatency();
    int GetCurrentPadding(out uint padding);
    int IsFormatSupported(int shareMode, IntPtr format, IntPtr closest);
    int GetMixFormat(out IntPtr format);
    int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr eventHandle);
    [PreserveSig] int GetService([MarshalAs(UnmanagedType.LPStruct)] Guid riid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
}

[ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioCaptureClient
{
    [PreserveSig] int GetBuffer(out IntPtr data, out uint numFrames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
    [PreserveSig] int ReleaseBuffer(uint numFrames);
    [PreserveSig] int GetNextPacketSize(out uint numFrames);
}

class CompletionHandler : IActivateAudioInterfaceCompletionHandler, IAgileObject
{
    public readonly ManualResetEvent Done = new ManualResetEvent(false);
    public int Result;
    public object Client;

    public void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op)
    {
        try { op.GetActivateResult(out Result, out Client); }
        catch (Exception e) { Result = Marshal.GetHRForException(e); }
        Done.Set();
    }
}

static class Program
{
    static WaveFormatEx Pcm16(uint rate, ushort channels)
    {
        var f = new WaveFormatEx();
        f.wFormatTag = 1; // WAVE_FORMAT_PCM
        f.nChannels = channels;
        f.nSamplesPerSec = rate;
        f.wBitsPerSample = 16;
        f.nBlockAlign = (ushort)(channels * 2);
        f.nAvgBytesPerSec = rate * f.nBlockAlign;
        f.cbSize = 0;
        return f;
    }

    static int LoopbackMode = Native.PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;

    static IAudioClient Activate(uint pid)
    {
        var p = new AudioClientActivationParams();
        p.ActivationType = Native.AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
        p.TargetProcessId = pid;
        p.ProcessLoopbackMode = LoopbackMode;

        IntPtr pParams = Marshal.AllocHGlobal(Marshal.SizeOf(p));
        IntPtr pVariant = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(PropVariantBlob)));
        try
        {
            Marshal.StructureToPtr(p, pParams, false);
            var v = new PropVariantBlob();
            v.vt = Native.VT_BLOB;
            v.cbSize = (uint)Marshal.SizeOf(p);
            v.pBlobData = pParams;
            Marshal.StructureToPtr(v, pVariant, false);

            var handler = new CompletionHandler();
            IActivateAudioInterfaceAsyncOperation op;
            Native.ActivateAudioInterfaceAsync("VAD\\Process_Loopback", typeof(IAudioClient).GUID, pVariant, handler, out op);
            if (!handler.Done.WaitOne(10000)) throw new Exception("Timeout ao ativar a captura de audio.");
            if (handler.Result != 0) throw new Exception(string.Format("Falha ao ativar captura (HRESULT 0x{0:X8}).", handler.Result));
            GC.KeepAlive(op);
            return (IAudioClient)handler.Client;
        }
        finally
        {
            Marshal.FreeHGlobal(pParams);
            Marshal.FreeHGlobal(pVariant);
        }
    }

    [MTAThread]
    static int Main(string[] args)
    {
        uint pid;
        if (args.Length < 1 || !uint.TryParse(args[0], out pid))
        {
            Console.Error.WriteLine("Uso: ProcAudioCapture.exe <PID>");
            return 2;
        }

        try
        {
            uint flags = Native.AUDCLNT_STREAMFLAGS_LOOPBACK | Native.AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                         Native.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | Native.AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;

            // Pede 16 kHz mono direto; se o sistema recusar, usa 48 kHz estereo e converte aqui.
            bool force48k = Array.IndexOf(args, "--48k") > 0;
            if (Array.IndexOf(args, "--exclude") > 0) LoopbackMode = 1; // captura tudo menos esse processo
            IAudioClient client = Activate(pid);
            WaveFormatEx fmt = Pcm16(16000, 1);
            int hr = force48k ? -1 : client.Initialize(0, flags, 2000000, 0, ref fmt, IntPtr.Zero);
            if (hr != 0)
            {
                Marshal.ReleaseComObject(client);
                client = Activate(pid);
                fmt = Pcm16(48000, 2);
                hr = client.Initialize(0, flags, 2000000, 0, ref fmt, IntPtr.Zero);
                if (hr != 0) throw new Exception(string.Format("IAudioClient.Initialize falhou (HRESULT 0x{0:X8}).", hr));
            }
            int decimate = (int)(fmt.nSamplesPerSec / 16000);
            int channels = fmt.nChannels;

            var ev = new AutoResetEvent(false);
            client.SetEventHandle(ev.SafeWaitHandle.DangerousGetHandle());

            object svc;
            hr = client.GetService(typeof(IAudioCaptureClient).GUID, out svc);
            if (hr != 0) throw new Exception(string.Format("GetService falhou (HRESULT 0x{0:X8}).", hr));
            var capture = (IAudioCaptureClient)svc;

            hr = client.Start();
            if (hr != 0) throw new Exception(string.Format("Start falhou (HRESULT 0x{0:X8}).", hr));
            Console.Error.WriteLine(string.Format("OK capturando PID {0} ({1} Hz, {2} canais)", pid, fmt.nSamplesPerSec, channels));

            Stream stdout = Console.OpenStandardOutput();
            byte[] raw = new byte[0];
            byte[] outBuf = new byte[0];

            while (true)
            {
                ev.WaitOne(200);
                uint packet;
                while (capture.GetNextPacketSize(out packet) == 0 && packet > 0)
                {
                    IntPtr data; uint frames, bufFlags; ulong devPos, qpc;
                    if (capture.GetBuffer(out data, out frames, out bufFlags, out devPos, out qpc) != 0) break;

                    int bytes = (int)frames * fmt.nBlockAlign;
                    if (raw.Length < bytes) raw = new byte[bytes];
                    if ((bufFlags & Native.AUDCLNT_BUFFERFLAGS_SILENT) != 0) Array.Clear(raw, 0, bytes);
                    else Marshal.Copy(data, raw, 0, bytes);
                    capture.ReleaseBuffer(frames);

                    int outCount;
                    if (decimate == 1 && channels == 1)
                    {
                        stdout.Write(raw, 0, bytes);
                        continue;
                    }
                    // Mistura canais e reduz para 16 kHz (media simples por bloco).
                    int outFrames = (int)frames / decimate;
                    outCount = outFrames * 2;
                    if (outBuf.Length < outCount) outBuf = new byte[outCount];
                    for (int i = 0; i < outFrames; i++)
                    {
                        int sum = 0;
                        for (int d = 0; d < decimate; d++)
                            for (int c = 0; c < channels; c++)
                                sum += BitConverter.ToInt16(raw, ((i * decimate + d) * channels + c) * 2);
                        short s = (short)(sum / (decimate * channels));
                        outBuf[i * 2] = (byte)(s & 0xFF);
                        outBuf[i * 2 + 1] = (byte)((s >> 8) & 0xFF);
                    }
                    stdout.Write(outBuf, 0, outCount);
                }
                stdout.Flush();
            }
        }
        catch (IOException)
        {
            return 0; // o programa que lia o stdout fechou
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("ERRO: " + e.Message);
            return 1;
        }
    }
}
