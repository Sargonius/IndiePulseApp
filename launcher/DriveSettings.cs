using System;
using System.IO;
using System.Text;

namespace IndiePulse
{
    // IndiePulse.portable.ini: optional settings of the drive IndiePulse is carried on, read as
    // app/core/drive_settings.py reads it (tests/test_launcher.py compares the two). Next to IndiePulse.exe, else in
    // the root of its drive; the first one found counts, none: the defaults. Lines of `key = value`, keys in any case,
    // a line that starts with # or ; is a comment, UTF-8 with or without a BOM; what it doesn't know is left out.
    //
    //   portable = yes|no          portable as with portable.txt (data\ next to the exe); the setup preselects Portable
    //   uv = <uv.exe>              the uv to use, before downloading one into runtime\uv
    //   uv_cache_dir = <folder>    UV_CACHE_DIR, unless the environment already has one (else runtime\cache)
    //   uv_python_dir = <folder>   UV_PYTHON_INSTALL_DIR, the same (else runtime\python)
    //   trusted_pcs = <file>       the computers that may get shortcuts and the autostart entry (the tray reads it)
    //
    // A path: %VARS% expanded; \folder from the root of the drive the ini is on, whatever its letter today; folder or
    // ..\folder from the ini's folder; X:\folder as written.
    sealed class DriveSettings
    {
        public const string FileName = "IndiePulse.portable.ini";
        static readonly string[] Yes = { "yes", "true", "on", "1" }, No = { "no", "false", "off", "0" };

        // the ini (null: none)
        public readonly string Ini;
        public readonly bool Portable;
        // full paths; null: not set
        public readonly string Uv, UvCacheDir, UvPythonDir, TrustedPcs;

        DriveSettings() { }

        DriveSettings(string ini)
        {
            Ini = ini;
            string text;
            try { text = File.ReadAllText(ini, Encoding.UTF8); }
            catch (IOException) { return; }
            catch (UnauthorizedAccessException) { return; }
            foreach (string raw in text.Split('\n'))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line[0] == '#' || line[0] == ';') continue;
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string key = line.Substring(0, eq).Trim().ToLowerInvariant();
                string value = Unquote(line.Substring(eq + 1).Trim());
                if (key == "portable")
                {
                    string v = value.ToLowerInvariant();
                    if (Array.IndexOf(Yes, v) >= 0) Portable = true;
                    else if (Array.IndexOf(No, v) >= 0) Portable = false;
                }
                else if (key == "uv") Uv = PathOf(value, ini);
                else if (key == "uv_cache_dir") UvCacheDir = PathOf(value, ini);
                else if (key == "uv_python_dir") UvPythonDir = PathOf(value, ini);
                else if (key == "trusted_pcs") TrustedPcs = PathOf(value, ini);
            }
        }

        // The settings for the IndiePulse folder `home`
        public static DriveSettings For(string home)
        {
            foreach (string dir in new[] { home, Path.GetPathRoot(home) })
            {
                string ini = Path.Combine(dir, FileName);
                if (File.Exists(ini)) return new DriveSettings(ini);
            }
            return new DriveSettings();
        }

        static string Unquote(string value)
        {
            bool quoted = value.Length >= 2 && value[0] == '"' && value[value.Length - 1] == '"';
            return quoted ? value.Substring(1, value.Length - 2) : value;
        }

        // A path as the ini writes it, made full; null: empty, or it can't be a path
        static string PathOf(string value, string ini)
        {
            if (value.Length == 0) return null;
            string v = Environment.ExpandEnvironmentVariables(value).Replace('/', '\\');
            if (v.IndexOfAny(Path.GetInvalidPathChars()) >= 0) return null;
            try
            {
                // X:\... and \\server\... stay as they are
                string full = v.StartsWith("\\") && !v.StartsWith("\\\\")
                    ? Path.Combine(Path.GetPathRoot(ini), v.TrimStart('\\'))
                    : Path.Combine(Path.GetDirectoryName(ini), v);
                return Home.Normalize(full);
            }
            catch (ArgumentException) { return null; }
            catch (NotSupportedException) { return null; }
            catch (PathTooLongException) { return null; }
        }
    }
}
