using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace IndiePulse
{
    // What IndiePulse runs on, prepared the way app/desktop/deps.py would: uv, Python 3.12 from uv, the venv,
    // requirements.lock in it. Nothing system-wide: Python goes into runtime\python (or ClubKit's folder on its drive)
    // without the python3.12.exe shim in the profile (--no-bin) and without a registry entry (--no-registry).
    //
    // uv: ClubKit's own copy when it is on the drive, else a pinned release downloaded from GitHub and checked
    // against its SHA-256 below (not bundled: the first run downloads Python and the packages anyway, so a bundled uv
    // would only make every zip and every update 15 MB bigger).
    sealed class Runtime
    {
        public const string UvVersion = "0.12.24";
        const string UvAsset = "uv-x86_64-pc-windows-msvc.zip";
        // GitHub's digest of that asset (the same as the release's .sha256 file); a new UvVersion needs its own
        const string UvSha256 = "7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019";
        const string UvUrl = "https://github.com/astral-sh/uv/releases/download/" + UvVersion + "/" + UvAsset;
        public const string PythonVersion = "3.12";   // the one requirements.lock is tested with
        const long MaxDownload = 100L * 1024 * 1024;

        readonly Home home;

        public Runtime(Home home)
        {
            this.home = home;
        }

        string KitUv { get { return Path.Combine(home.Kit, "tools", "uv", "uv.exe"); } }
        string OwnUv { get { return Path.Combine(home.Runtime, "uv", "uv.exe"); } }

        // The uv to use ("" = none yet): the same order as deps.uv()
        public string Uv
        {
            get
            {
                if (File.Exists(KitUv)) return KitUv;
                return File.Exists(OwnUv) ? OwnUv : "";
            }
        }

        // What has to happen before IndiePulse can start; empty: nothing
        public List<Step> Plan()
        {
            var steps = new List<Step>();
            bool alive = home.VenvAlive();
            if (alive && home.StampCurrent()) return steps;
            bool pip = alive && File.Exists(Path.Combine(home.Venv, "Scripts", "pip.exe"));
            if (Uv.Length == 0 && !pip) steps.Add(new Step("uv", "Getting uv, the Python installer", GetUv));
            if (!alive)
            {
                steps.Add(new Step("python", "Getting Python " + PythonVersion, GetPython));
                steps.Add(new Step("venv", "Making IndiePulse's Python environment", MakeVenv));
            }
            steps.Add(new Step("packages", "Installing the packages IndiePulse uses", InstallPackages));
            return steps;
        }

        // All of it, for a setup: each step finds out by itself whether there is anything to do (the code may not
        // be in place yet when this is put together)
        public List<Step> AllSteps()
        {
            return new List<Step>
            {
                new Step("uv", "Getting uv, the Python installer", GetUv),
                new Step("python", "Getting Python " + PythonVersion, GetPython),
                new Step("venv", "Making IndiePulse's Python environment", MakeVenv),
                new Step("packages", "Installing the packages IndiePulse uses", InstallPackages),
            };
        }

        // ── uv ───────────────────────────────────────────────────────────────
        void GetUv(StepContext ctx)
        {
            if (Uv.Length > 0) return;
            string dir = Path.GetDirectoryName(OwnUv);
            Directory.CreateDirectory(dir);
            string zip = Path.Combine(dir, UvAsset + ".part");
            Download(UvUrl, zip, UvSha256, "uv " + UvVersion, ctx);
            ctx.Progress(-1);
            ctx.Detail("Unpacking uv…");
            string part = OwnUv + ".part";
            try
            {
                using (ZipArchive z = ZipFile.OpenRead(zip))
                {
                    ZipArchiveEntry entry = z.GetEntry("uv.exe");
                    if (entry == null) throw new StepFailed("The uv download has no uv.exe in it.");
                    entry.ExtractToFile(part, true);
                }
                if (File.Exists(OwnUv)) File.Delete(OwnUv);
                File.Move(part, OwnUv);
            }
            catch (InvalidDataException ex)
            {
                throw new StepFailed("The uv download isn't a valid zip.", ex);
            }
            finally
            {
                TryDelete(zip);
                TryDelete(part);
            }
            Thread.Sleep(300);   // an antivirus looks at a new exe first; quarantined, it is gone now
            if (!File.Exists(OwnUv))
                throw new StepFailed("uv.exe was downloaded, then disappeared: an antivirus may have quarantined it. " +
                    "Allow " + OwnUv + " and Retry.");
            int code = RunTool(OwnUv, "--version", null, ctx);
            if (code != 0)
                throw new StepFailed("uv.exe was downloaded but doesn't start (code " + code + "): an antivirus may be blocking it.");
        }

        void Download(string url, string file, string sha256, string what, StepContext ctx)
        {
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            ctx.Log.Write("downloading " + url);
            ctx.Detail("Downloading " + what + "…");
            string hash;
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(url);
                req.UserAgent = "IndiePulse-setup/" + home.Version;
                req.Timeout = 30000;
                req.ReadWriteTimeout = 60000;
                using (var resp = (HttpWebResponse)req.GetResponse())
                using (Stream body = resp.GetResponseStream())
                using (FileStream f = File.Create(file))
                using (SHA256 sha = SHA256.Create())
                {
                    long total = resp.ContentLength, done = 0;
                    var buf = new byte[81920];
                    int n, shown = -1;
                    while ((n = body.Read(buf, 0, buf.Length)) > 0)
                    {
                        f.Write(buf, 0, n);
                        sha.TransformBlock(buf, 0, n, null, 0);
                        done += n;
                        if (done > MaxDownload) throw new StepFailed(what + " is far bigger than it should be: not used.");
                        int pct = total > 0 ? (int)(done * 100 / total) : -1;
                        if (pct != shown)
                        {
                            shown = pct;
                            ctx.Progress(pct);
                            ctx.Detail(total > 0
                                ? string.Format("Downloading {0}: {1:0.0} of {2:0.0} MB", what, done / 1048576.0, total / 1048576.0)
                                : string.Format("Downloading {0}: {1:0.0} MB", what, done / 1048576.0));
                        }
                    }
                    sha.TransformFinalBlock(buf, 0, 0);
                    hash = Home.Hex(sha.Hash);
                }
            }
            catch (WebException ex)
            {
                TryDelete(file);
                throw new StepFailed("Couldn't download " + what + " (" + ex.Message + "). Check the internet connection, then Retry.", ex);
            }
            catch (IOException ex)
            {
                TryDelete(file);
                throw new StepFailed("Couldn't download " + what + " (" + ex.Message + "). Retry.", ex);
            }
            if (hash != sha256)
            {
                TryDelete(file);
                ctx.Log.Write("sha256 " + hash + ", expected " + sha256);
                throw new StepFailed("The downloaded " + what + " doesn't match its checksum, so it isn't used (a damaged " +
                    "download, or something on the way changed it). Retry.");
            }
            ctx.Log.Write("sha256 ok: " + hash);
        }

        // ── Python, the venv, the packages ───────────────────────────────────
        string InstallPython(bool reinstall)
        {
            return "python install --no-bin --no-registry" + (reinstall ? " --reinstall" : "") + " " + PythonVersion;
        }

        void GetPython(StepContext ctx)
        {
            if (home.VenvAlive()) return;
            ctx.Progress(-1);
            ctx.Detail("Downloading Python " + PythonVersion + "…");
            // a venv whose Python lost its python.exe (an antivirus took it): put back from uv's cache
            RunUv(InstallPython(home.PythonDamaged()), "Python " + PythonVersion, ctx);
        }

        void MakeVenv(StepContext ctx)
        {
            ctx.Progress(-1);
            if (home.VenvAlive())
            {
                ctx.Detail("The environment is fine.");
                return;
            }
            try { Files.DeleteTree(home.Venv); }
            catch (IOException ex) { ctx.Log.Write("old venv", ex); }   // --clear takes what's left
            string args = "venv --seed --clear --python " + PythonVersion + " " + Quote(home.Venv);
            ctx.Detail("Making the environment…");
            if (RunUvCode(args, ctx) != 0)
            {
                // uv's own Python may be the broken part: once more after putting it back
                RunUv(InstallPython(true), "Python " + PythonVersion, ctx);
                RunUv(args, "the environment", ctx);
            }
            if (!home.VenvAlive())
                throw new StepFailed("The environment was made but its Python isn't there: an antivirus may have taken " +
                    "python.exe. See the log, then Retry.");
        }

        void InstallPackages(StepContext ctx)
        {
            ctx.Progress(-1);
            if (home.StampCurrent()) return;
            string list = home.InstallList;
            ctx.Detail("Installing packages…");
            string uv = Uv;
            if (uv.Length > 0)
            {
                RunUv("pip install --python " + Quote(home.VenvPython(false)) + " -r " + Quote(list), "the packages", ctx);
            }
            else
            {
                int code = RunTool(home.VenvPython(false), "-m pip install --disable-pip-version-check -r " + Quote(list), null, ctx);
                if (code != 0) throw new StepFailed("Couldn't install the packages (pip said code " + code + "). See the log, then Retry.");
            }
            File.WriteAllText(home.Stamp, home.RequirementsHash(), Encoding.ASCII);
        }

        // ── starting ─────────────────────────────────────────────────────────
        // The tray, in the venv: `pythonw -m app.desktop.tray`, from the code folder (where `app` is imported from)
        public Process StartTray(bool background)
        {
            var psi = new ProcessStartInfo(home.VenvPython(true), "-m app.desktop.tray" + (background ? " --background" : ""));
            psi.WorkingDirectory = home.Root;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.Environment["INDIEPULSE_INSTANCE"] = home.Key;
            return Process.Start(psi);
        }

        // Start, and wait until the server answers (the tray sets "ready" as it opens the window)
        public void StartAndWait(StepContext ctx, bool background)
        {
            ctx.Progress(-1);
            ctx.Detail("The first start takes a little longer…");
            using (EventWaitHandle ready = Instance.CreateReady(home))
            using (var exited = new ManualResetEvent(false))
            {
                Process p = StartTray(background);
                ctx.Log.Write("tray started, pid " + p.Id);
                p.EnableRaisingEvents = true;
                p.Exited += delegate { try { exited.Set(); } catch (ObjectDisposedException) { } };
                if (p.HasExited) exited.Set();
                int which = WaitHandle.WaitAny(new WaitHandle[] { ready, exited }, TimeSpan.FromMinutes(3));
                if (which == 0)
                {
                    ctx.Log.Write("IndiePulse is up");
                    return;
                }
                ctx.LogToOpen = Path.Combine(home.Logs, "launcher.log");
                if (which == 1)
                {
                    ctx.Log.Write("the tray exited at start, code " + p.ExitCode);
                    throw new StepFailed("IndiePulse stopped while starting (code " + p.ExitCode + "). Its log says why: " +
                        "logs\\launcher.log in " + home.Data + ".");
                }
                throw new StepFailed("IndiePulse hasn't answered in 3 minutes. Its icon in the notification area says " +
                    "how it's doing; the logs are in " + home.Logs + ".");
            }
        }

        // ── running tools ────────────────────────────────────────────────────
        void ApplyUvEnv(IDictionary<string, string> env, bool systemCerts)
        {
            if (home.KitPresent)
            {
                // ClubKit's own folders on the drive, unless its scripts already said where (deps.uv_env())
                SetDefault(env, "UV_CACHE_DIR", Path.Combine(home.Kit, "data", "uv", "cache"));
                SetDefault(env, "UV_PYTHON_INSTALL_DIR", Path.Combine(home.Kit, "data", "uv", "python"));
                SetDefault(env, "UV_PYTHON_PREFERENCE", "only-managed");
            }
            else
            {
                // everything in runtime\: a portable copy leaves nothing in the profile, an uninstall takes it all
                env["UV_CACHE_DIR"] = Path.Combine(home.Runtime, "cache");
                env["UV_PYTHON_INSTALL_DIR"] = Path.Combine(home.Runtime, "python");
                env["UV_PYTHON_PREFERENCE"] = "only-managed";
            }
            env["NO_COLOR"] = "1";
            env["UV_NO_PROGRESS"] = "1";
            if (systemCerts)
            {
                // the TLS certificates of Windows' own store (an antivirus or a company proxy that inspects HTTPS
                // puts its root there): the name differs between uv versions
                env["UV_SYSTEM_CERTS"] = "1";
                env["UV_NATIVE_TLS"] = "1";
            }
        }

        static void SetDefault(IDictionary<string, string> env, string key, string value)
        {
            string have;
            if (!env.TryGetValue(key, out have) || string.IsNullOrEmpty(have)) env[key] = value;
        }

        int RunUvCode(string args, StepContext ctx)
        {
            string uv = Uv;
            if (uv.Length == 0) throw new StepFailed("uv is missing (" + OwnUv + "): Retry gets it again.");
            var output = new StringBuilder();
            int code = RunTool(uv, args, delegate(IDictionary<string, string> env) { ApplyUvEnv(env, false); }, ctx, output);
            string text = output.ToString();
            if (code != 0 && (text.IndexOf("certificate", StringComparison.OrdinalIgnoreCase) >= 0
                              || text.IndexOf("UnknownIssuer", StringComparison.OrdinalIgnoreCase) >= 0))
            {
                ctx.Log.Write("a TLS certificate problem: once more with Windows' certificate store");
                code = RunTool(uv, args, delegate(IDictionary<string, string> env) { ApplyUvEnv(env, true); }, ctx, null);
            }
            return code;
        }

        void RunUv(string args, string what, StepContext ctx)
        {
            int code = RunUvCode(args, ctx);
            if (code != 0)
                throw new StepFailed("Couldn't get " + what + " (uv said code " + code + "). Check the internet " +
                    "connection; the log has the details. Then Retry.");
        }

        int RunTool(string exe, string args, Action<IDictionary<string, string>> env, StepContext ctx)
        {
            return RunTool(exe, args, env, ctx, null);
        }

        // A tool, hidden, its output into the log and its last line under the progress bar
        int RunTool(string exe, string args, Action<IDictionary<string, string>> env, StepContext ctx, StringBuilder output)
        {
            var psi = new ProcessStartInfo(exe, args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;
            psi.WorkingDirectory = Path.GetTempPath();   // nowhere a pyproject.toml or .python-version would change what uv does
            if (env != null) env(psi.Environment);
            ctx.Log.Write("> " + Path.GetFileName(exe) + " " + args);
            DataReceivedEventHandler onLine = delegate(object sender, DataReceivedEventArgs e)
            {
                if (string.IsNullOrEmpty(e.Data)) return;
                ctx.Log.Write("  " + e.Data);
                if (output != null) lock (output) output.AppendLine(e.Data);
                string line = e.Data.Trim();
                if (line.Length > 0 && !line.StartsWith("+") && !line.StartsWith("-")) ctx.Detail(line);
            };
            using (var p = new Process())
            {
                p.StartInfo = psi;
                p.OutputDataReceived += onLine;
                p.ErrorDataReceived += onLine;
                try { p.Start(); }
                catch (System.ComponentModel.Win32Exception ex)
                {
                    throw new StepFailed("Couldn't start " + exe + " (" + ex.Message + "): an antivirus may be blocking it.", ex);
                }
                ctx.Child = p;
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                p.WaitForExit();
                ctx.Child = null;
                ctx.Log.Write("  (code " + p.ExitCode + ")");
                return p.ExitCode;
            }
        }

        public static string Quote(string arg)
        {
            return "\"" + arg.TrimEnd('\\') + "\"";
        }

        static void TryDelete(string file)
        {
            try { if (File.Exists(file)) File.Delete(file); }
            catch (Exception) { }
        }
    }
}
