using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace IndiePulse
{
    // One IndiePulse folder and what follows from it. The answers are the same as the Python side's
    // (app/core/paths.py: HOME, ROOT, RUNTIME, DATA, DRIVE; app/desktop/deps.py: the venv, its stamp) - a change
    // there is a change here, and tests/test_launcher.py compares the two.
    sealed class Home
    {
        public const string ExeName = "IndiePulse.exe";

        // HOME: the folder with IndiePulse.exe, what the user sees
        public readonly string Dir;
        // ROOT: the code, the folder app\ is in: lib\ of a release, the folder itself in a checkout
        public readonly string Root;
        // a release (IndiePulse.exe + lib\), not a checkout
        public readonly bool Release;
        // uv, Python and a release's venv, prepared here (Runtime.cs)
        public readonly string Runtime;
        // the venv: runtime\venv of a release (lib\ is replaced whole by an update), .venv of a checkout
        public readonly string Venv;
        // IndiePulse.portable.ini next to the exe or in the root of its drive: portable, the uv to use and uv's
        // folders (DriveSettings.cs)
        public readonly DriveSettings Drive;

        // The layout as the folder has it (lib\app\ there: a release)
        public Home(string dir) : this(dir, File.Exists(Path.Combine(Normalize(dir), "lib", "app", "__init__.py"))) { }

        // `release`: for a folder the release is about to be copied into
        public Home(string dir, bool release)
        {
            Dir = Normalize(dir);
            Release = release;
            Root = Release ? Path.Combine(Dir, "lib") : Dir;
            Runtime = Path.Combine(Dir, "runtime");
            Venv = Release ? Path.Combine(Runtime, "venv") : Path.Combine(Root, ".venv");
            Drive = DriveSettings.For(Dir);
        }

        public static Home OfThisExe()
        {
            return new Home(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
        }

        public string Exe { get { return Path.Combine(Dir, ExeName); } }
        public bool HasCode { get { return File.Exists(Path.Combine(Root, "app", "__init__.py")); } }
        public string PortableMarker { get { return Path.Combine(Dir, "portable.txt"); } }

        // Portable: portable.txt (the setup's choice) or `portable = yes` in the drive settings - data\ next to the exe
        public bool Portable { get { return File.Exists(PortableMarker) || Drive.Portable; } }

        // DATA, the same rules as paths._data_dir()
        public string Data
        {
            get
            {
                string env = Environment.GetEnvironmentVariable("INDIEPULSE_DATA");
                if (!string.IsNullOrEmpty(env)) return Normalize(env);
                if (File.Exists(Path.Combine(Root, "indiepulse.db")) || File.Exists(Path.Combine(Root, ".settings.json")))
                    return Root;
                if (Portable) return Path.Combine(Dir, "data");
                string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
                return string.IsNullOrEmpty(local) ? Path.Combine(Dir, "data") : Path.Combine(Normalize(local), "IndiePulse");
            }
        }

        public string Logs { get { return Path.Combine(Data, "logs"); } }
        // the app window's own browser profile (app/desktop/window.py): browser\ is Edge's, another browser's is
        // browser-<its exe> next to it (browser-chrome)
        public string BrowserProfile { get { return Path.Combine(Data, "browser"); } }

        // A browser's command line with --user-data-dir= one of the window's profiles, whichever browser (one left from
        // before the default browser changed too): window.is_profile()
        public bool UsesBrowserProfile(string commandLine)
        {
            if (string.IsNullOrEmpty(commandLine)) return false;
            // "--user-data-dir=C:\a b\browser" and --user-data-dir="C:\a b\browser" alike
            string cmd = commandLine.Replace("\"", "");
            string needle = "--user-data-dir=" + BrowserProfile;
            int at = cmd.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
            while (at >= 0)
            {
                int end = at + needle.Length;
                if (end == cmd.Length || char.IsWhiteSpace(cmd[end])) return true;
                if (cmd[end] == '-')
                {
                    int stop = end + 1;
                    while (stop < cmd.Length && !char.IsWhiteSpace(cmd[stop]) && cmd[stop] != '\\' && cmd[stop] != '/') stop++;
                    if (stop > end + 1 && (stop == cmd.Length || char.IsWhiteSpace(cmd[stop]))) return true;
                }
                at = cmd.IndexOf(needle, end, StringComparison.OrdinalIgnoreCase);
            }
            return false;
        }

        public string Version
        {
            get
            {
                try
                {
                    string text = File.ReadAllText(Path.Combine(Root, "app", "__init__.py"));
                    Match m = Regex.Match(text, "^__version__\\s*=\\s*\"([^\"]+)\"", RegexOptions.Multiline);
                    return m.Success ? m.Groups[1].Value : "";
                }
                catch (IOException) { return ""; }
                catch (UnauthorizedAccessException) { return ""; }
            }
        }

        // What the named events and mutexes of this folder are called by (Instance.cs). Passed to the tray as
        // INDIEPULSE_INSTANCE; apply_update.instance_key() computes the same for a tray started without it.
        public string Key
        {
            get
            {
                string full = LongPath(Dir).ToLowerInvariant();
                using (var sha = SHA1.Create())
                {
                    byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(full));
                    return Hex(hash).Substring(0, 16);
                }
            }
        }

        // ── the venv ─────────────────────────────────────────────────────────
        public string VenvPython(bool windowed)
        {
            return Path.Combine(Venv, "Scripts", windowed ? "pythonw.exe" : "python.exe");
        }

        public string Requirements { get { return Path.Combine(Root, "requirements.txt"); } }
        public string Lock { get { return Path.Combine(Root, "requirements.lock"); } }
        // what gets installed: the lock (exact versions), else requirements.txt
        public string InstallList { get { return File.Exists(Lock) ? Lock : Requirements; } }
        public string Stamp { get { return Path.Combine(Venv, "indiepulse-requirements.sha256"); } }

        // deps._requirements_hash(): sha256 of requirements.txt followed by requirements.lock
        public string RequirementsHash()
        {
            using (var sha = SHA256.Create())
            using (var all = new MemoryStream())
            {
                byte[] req = File.ReadAllBytes(Requirements);
                all.Write(req, 0, req.Length);
                if (File.Exists(Lock))
                {
                    byte[] lck = File.ReadAllBytes(Lock);
                    all.Write(lck, 0, lck.Length);
                }
                return Hex(sha.ComputeHash(all.ToArray()));
            }
        }

        public bool StampCurrent()
        {
            try { return File.ReadAllText(Stamp, Encoding.ASCII).Trim() == RequirementsHash(); }
            catch (IOException) { return false; }
            catch (UnauthorizedAccessException) { return false; }
        }

        // The base Python the venv's pyvenv.cfg names ("" without a venv)
        public string VenvHome()
        {
            try
            {
                foreach (string line in File.ReadAllLines(Path.Combine(Venv, "pyvenv.cfg")))
                {
                    int eq = line.IndexOf('=');
                    if (eq > 0 && line.Substring(0, eq).Trim() == "home") return line.Substring(eq + 1).Trim();
                }
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            return "";
        }

        // deps.venv_alive(): the venv runs on this computer - the base Python it was made on is here, with python.exe
        // (a venv from another computer, or one whose Python an antivirus took, still has its own python.exe)
        public bool VenvAlive()
        {
            string home = VenvHome();
            return home.Length > 0 && File.Exists(Path.Combine(home, "python.exe")) && File.Exists(VenvPython(true));
        }

        // The venv's Python lost its python.exe but its folder is there: uv puts it back (--reinstall)
        public bool PythonDamaged()
        {
            string home = VenvHome();
            return home.Length > 0 && Directory.Exists(home) && !File.Exists(Path.Combine(home, "python.exe"));
        }

        // ── helpers ──────────────────────────────────────────────────────────
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern uint GetLongPathName(string shortPath, StringBuilder longPath, uint size);

        // A full path without a trailing backslash (but "C:\"), short 8.3 names spelt out
        public static string Normalize(string path)
        {
            string full = Path.GetFullPath(path);
            if (full.Length > 3) full = full.TrimEnd('\\');
            return LongPath(full);
        }

        static string LongPath(string path)
        {
            var sb = new StringBuilder(1024);
            uint n = GetLongPathName(path, sb, (uint)sb.Capacity);
            return n > 0 && n < sb.Capacity ? sb.ToString() : path;
        }

        public static string Hex(byte[] bytes)
        {
            var sb = new StringBuilder(bytes.Length * 2);
            foreach (byte b in bytes) sb.Append(b.ToString("x2"));
            return sb.ToString();
        }

        public static bool SamePath(string a, string b)
        {
            if (string.IsNullOrEmpty(a) || string.IsNullOrEmpty(b)) return false;
            try { return string.Equals(Normalize(a), Normalize(b), StringComparison.OrdinalIgnoreCase); }
            catch (Exception) { return false; }
        }

        // `path` is `dir` or inside it
        public static bool Inside(string path, string dir)
        {
            if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(dir)) return false;
            string p = path.TrimEnd('\\') + "\\", d = dir.TrimEnd('\\') + "\\";
            return p.StartsWith(d, StringComparison.OrdinalIgnoreCase);
        }
    }

    // The order of versions, as app/services/updates.py parse_version: 2.0.0-dev < 2.0.0-rc.1 < 2.0.0 < 2.0.1-dev
    static class Versions
    {
        static readonly Regex Rx = new Regex("^v?(\\d+)\\.(\\d+)\\.(\\d+)(?:[-.]?(dev|alpha|a|beta|b|rc)[.-]?(\\d*))?$",
            RegexOptions.IgnoreCase);

        static long[] Parse(string v)
        {
            Match m = Rx.Match((v ?? "").Trim());
            if (!m.Success) return null;
            int stage = 4;
            string s = m.Groups[4].Value.ToLowerInvariant();
            if (s == "dev") stage = 0;
            else if (s == "alpha" || s == "a") stage = 1;
            else if (s == "beta" || s == "b") stage = 2;
            else if (s == "rc") stage = 3;
            long n = m.Groups[5].Value.Length > 0 ? long.Parse(m.Groups[5].Value) : 0;
            return new long[] { long.Parse(m.Groups[1].Value), long.Parse(m.Groups[2].Value), long.Parse(m.Groups[3].Value), stage, n };
        }

        // <0, 0, >0; an unreadable version counts as the oldest
        public static int Compare(string a, string b)
        {
            long[] x = Parse(a), y = Parse(b);
            if (x == null || y == null) return (x == null ? 0 : 1) - (y == null ? 0 : 1);
            for (int i = 0; i < x.Length; i++)
                if (x[i] != y[i]) return x[i] < y[i] ? -1 : 1;
            return 0;
        }
    }
}
