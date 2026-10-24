using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace IndiePulse
{
    // The user's folders. Windows' own (known folders: a desktop moved to OneDrive is found), except where the
    // environment has been pointed elsewhere - LOCALAPPDATA, APPDATA, USERPROFILE set to a sandbox - which is then
    // followed (and is what the Python side does too: it reads LOCALAPPDATA).
    static class Folders
    {
        static string Known(Environment.SpecialFolder f)
        {
            return Environment.GetFolderPath(f, Environment.SpecialFolderOption.DoNotVerify);
        }

        static string Env(string name)
        {
            string v = Environment.GetEnvironmentVariable(name);
            return string.IsNullOrEmpty(v) ? null : Home.Normalize(v);
        }

        static bool Moved(string env, Environment.SpecialFolder f)
        {
            string v = Env(env);
            return v != null && !Home.SamePath(v, Known(f));
        }

        public static string LocalAppData
        {
            get { return Env("LOCALAPPDATA") ?? Known(Environment.SpecialFolder.LocalApplicationData); }
        }

        public static string StartMenuPrograms
        {
            get
            {
                if (Moved("APPDATA", Environment.SpecialFolder.ApplicationData))
                    return Path.Combine(Env("APPDATA"), "Microsoft", "Windows", "Start Menu", "Programs");
                return Known(Environment.SpecialFolder.Programs);
            }
        }

        public static string Desktop
        {
            get
            {
                if (Moved("USERPROFILE", Environment.SpecialFolder.UserProfile))
                    return Path.Combine(Env("USERPROFILE"), "Desktop");
                return Known(Environment.SpecialFolder.DesktopDirectory);
            }
        }

        public static string Temp { get { return Path.GetTempPath(); } }

        // Where Install puts IndiePulse: the per-user programs folder (no admin rights needed)
        public static string InstallDir
        {
            get { return Path.Combine(LocalAppData, "Programs", "IndiePulse"); }
        }
    }

    // .lnk files through the shell's own IShellLink (no scripting host)
    static class Shortcut
    {
        [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
        class ShellLink { }

        [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
        interface IShellLinkW
        {
            void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int size, IntPtr findData, int flags);
            void GetIDList(out IntPtr idl);
            void SetIDList(IntPtr idl);
            void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int size);
            void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
            void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int size);
            void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
            void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int size);
            void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
            void GetHotkey(out short hotkey);
            void SetHotkey(short hotkey);
            void GetShowCmd(out int showCmd);
            void SetShowCmd(int showCmd);
            void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int icon);
            void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int icon);
            void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, int reserved);
            void Resolve(IntPtr hwnd, int flags);
            void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
        }

        public static void Create(string lnk, string target, string args, string workDir, string description)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(lnk));
            var link = (IShellLinkW)new ShellLink();
            try
            {
                link.SetPath(target);
                link.SetArguments(args ?? "");
                link.SetWorkingDirectory(workDir);
                link.SetIconLocation(target, 0);
                link.SetDescription(description);
                ((IPersistFile)link).Save(lnk, true);
            }
            finally { Marshal.ReleaseComObject(link); }
        }

        // What a shortcut starts ("" when it can't be read)
        public static string Target(string lnk)
        {
            if (!File.Exists(lnk)) return "";
            var link = (IShellLinkW)new ShellLink();
            try
            {
                ((IPersistFile)link).Load(lnk, 0);
                var sb = new StringBuilder(1024);
                link.GetPath(sb, sb.Capacity, IntPtr.Zero, 0);
                return sb.ToString();
            }
            catch (Exception) { return ""; }
            finally { Marshal.ReleaseComObject(link); }
        }
    }

    // Is a folder on a drive the user carries around? A USB stick says "removable"; a USB SSD or hard disk says
    // "fixed", but its bus is USB.
    static class Drives
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint disposition,
            uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool DeviceIoControl(SafeFileHandle device, uint code, ref StoragePropertyQuery input, int inputSize,
            byte[] output, int outputSize, out int returned, IntPtr overlapped);

        [StructLayout(LayoutKind.Sequential)]
        struct StoragePropertyQuery
        {
            public int PropertyId;   // StorageDeviceProperty = 0
            public int QueryType;    // PropertyStandardQuery = 0
            public byte Extra;
        }

        const uint IoctlStorageQueryProperty = 0x002D1400;
        const int BusTypeUsb = 7, BusTypeSd = 12, BusTypeMmc = 13;

        public static bool Removable(string path)
        {
            try
            {
                string root = Path.GetPathRoot(Path.GetFullPath(path));
                if (root.StartsWith("\\\\")) return false;   // a network share
                var info = new DriveInfo(root);
                if (info.DriveType == DriveType.Removable) return true;
                if (info.DriveType != DriveType.Fixed) return false;
                return UsbBus(root.Substring(0, 2));
            }
            catch (Exception) { return false; }
        }

        static bool UsbBus(string letter)
        {
            // no access asked for: a query of the device's properties needs none (and no admin rights)
            using (SafeFileHandle h = CreateFile("\\\\.\\" + letter, 0, 3, IntPtr.Zero, 3, 0, IntPtr.Zero))
            {
                if (h.IsInvalid) return false;
                var query = new StoragePropertyQuery();
                var output = new byte[1024];
                int got;
                if (!DeviceIoControl(h, IoctlStorageQueryProperty, ref query, Marshal.SizeOf(query), output, output.Length,
                        out got, IntPtr.Zero) || got < 32)
                    return false;
                // STORAGE_DEVICE_DESCRIPTOR: ... BusType (a DWORD) at offset 28
                int bus = BitConverter.ToInt32(output, 28);
                return bus == BusTypeUsb || bus == BusTypeSd || bus == BusTypeMmc;
            }
        }
    }

    static class Files
    {
        // A file copied over another, retried while something (an antivirus scan, the indexer) has it open
        public static void Copy(string src, string dst)
        {
            for (int attempt = 0; ; attempt++)
            {
                try
                {
                    if (File.Exists(dst)) File.SetAttributes(dst, FileAttributes.Normal);
                    File.Copy(src, dst, true);
                    return;
                }
                catch (IOException)
                {
                    if (attempt >= 20) throw;
                    Thread.Sleep(250);
                }
                catch (UnauthorizedAccessException)
                {
                    if (attempt >= 20) throw;
                    Thread.Sleep(250);
                }
            }
        }

        // Delete a folder with everything in it, read-only files too; retried a few times (a file just closed by a
        // process that ended is sometimes still held for a moment). `keep`: paths inside it that stay.
        public static void DeleteTree(string dir, params string[] keep)
        {
            if (!Directory.Exists(dir)) return;
            Exception last = null;
            for (int attempt = 0; attempt < 10; attempt++)
            {
                try
                {
                    DeleteInside(dir, keep);
                    if (!HoldsKept(dir, keep)) Directory.Delete(dir, false);
                    return;
                }
                catch (IOException ex) { last = ex; }
                catch (UnauthorizedAccessException ex) { last = ex; }
                Thread.Sleep(500);
            }
            throw new IOException("Couldn't delete " + dir + ": " + (last == null ? "?" : last.Message), last);
        }

        static bool Kept(string path, string[] keep)
        {
            foreach (string k in keep)
                if (Home.SamePath(path, k)) return true;
            return false;
        }

        static bool HoldsKept(string dir, string[] keep)
        {
            foreach (string k in keep)
                if (Home.Inside(k, dir) && !Home.SamePath(k, dir)) return true;
            return false;
        }

        static void DeleteInside(string dir, string[] keep)
        {
            foreach (string path in Directory.GetFileSystemEntries(dir))
            {
                if (Kept(path, keep)) continue;
                var attrs = File.GetAttributes(path);
                bool folder = (attrs & FileAttributes.Directory) != 0;
                if (folder && (attrs & FileAttributes.ReparsePoint) == 0)
                {
                    DeleteInside(path, keep);
                    if (!HoldsKept(path, keep)) Directory.Delete(path, false);
                }
                else if (folder)
                {
                    Directory.Delete(path, false);   // a junction (uv's 3.12 -> 3.12.x link): the link, not its target
                }
                else
                {
                    File.SetAttributes(path, FileAttributes.Normal);
                    File.Delete(path);
                }
            }
        }

        public static long Size(string dir)
        {
            long total = 0;
            try
            {
                foreach (string f in Directory.GetFiles(dir))
                {
                    try { total += new FileInfo(f).Length; }
                    catch (Exception) { }
                }
                foreach (string d in Directory.GetDirectories(dir))
                {
                    if ((File.GetAttributes(d) & FileAttributes.ReparsePoint) == 0) total += Size(d);
                }
            }
            catch (Exception) { }
            return total;
        }
    }
}
