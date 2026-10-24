using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using Microsoft.Win32;

namespace IndiePulse
{
    // Install for this user: the program into %LOCALAPPDATA%\Programs\IndiePulse, its data in
    // %LOCALAPPDATA%\IndiePulse (paths.py's rule for a copy that isn't portable), Start-menu and desktop shortcuts, an
    // entry under HKCU's Uninstall (Settings -> Apps), which runs `IndiePulse.exe --uninstall`. No admin rights: all
    // of it is the user's own. The autostart entry is the tray's ("Start at Windows sign-in"); the uninstall removes
    // it too.
    static class Installer
    {
        const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\IndiePulse";
        const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        const string Publisher = "Evgeny Nesterovskiy";
        const string Website = "https://github.com/Sargonius/IndiePulseNew";

        static string StartMenuLink { get { return Path.Combine(Folders.StartMenuPrograms, "IndiePulse.lnk"); } }
        static string DesktopLink { get { return Path.Combine(Folders.Desktop, "IndiePulse.lnk"); } }

        static string InstallLocation()
        {
            using (RegistryKey k = Registry.CurrentUser.OpenSubKey(UninstallKey))
                return k == null ? "" : (k.GetValue("InstallLocation") as string ?? "");
        }

        // The IndiePulse installed for this user (null: none, or its folder is gone)
        public static Home Installed()
        {
            string where = InstallLocation();
            if (where.Length == 0 || !Directory.Exists(where)) return null;
            var home = new Home(where);
            return File.Exists(home.Exe) && home.HasCode ? home : null;
        }

        public static bool IsInstalledAt(Home home)
        {
            return Home.SamePath(InstallLocation(), home.Dir);
        }

        // ── install ──────────────────────────────────────────────────────────
        // The release's files from `source` into `target`: the exe, README, LICENSE and lib\ (whole: a file an
        // older version had must not stay). runtime\ and the data stay where they are.
        public static void Copy(Home source, Home target, StepContext ctx)
        {
            if (Home.SamePath(source.Dir, target.Dir))
            {
                ctx.Detail("Already in place.");
                return;
            }
            Directory.CreateDirectory(target.Dir);
            string lib = Path.Combine(target.Dir, "lib"), fresh = lib + ".new", old = lib + ".old";
            Files.DeleteTree(fresh);
            var all = new List<string>(Directory.GetFiles(source.Root, "*", SearchOption.AllDirectories));
            int done = 0;
            foreach (string file in all)
            {
                string rel = file.Substring(source.Root.Length).TrimStart('\\');
                if (rel.IndexOf("__pycache__", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                string dst = Path.Combine(fresh, rel);
                Directory.CreateDirectory(Path.GetDirectoryName(dst));
                Files.Copy(file, dst);
                done++;
                if (done % 10 == 0 || done == all.Count)
                {
                    ctx.Progress(done * 100 / all.Count);
                    ctx.Detail("Copying " + rel);
                }
            }
            Files.DeleteTree(old);
            if (Directory.Exists(lib)) Directory.Move(lib, old);
            Directory.Move(fresh, lib);
            try { Files.DeleteTree(old); }
            catch (IOException ex) { ctx.Log.Write("the old lib stays for now", ex); }
            foreach (string name in new[] { Home.ExeName, "README.md", "LICENSE" })
            {
                string src = Path.Combine(source.Dir, name);
                if (File.Exists(src)) Files.Copy(src, Path.Combine(target.Dir, name));
            }
            ctx.Log.Write("copied " + done + " files to " + target.Dir);
        }

        // Shortcuts and the uninstall entry
        public static void Register(Home home, StepContext ctx)
        {
            Shortcut.Create(StartMenuLink, home.Exe, "", home.Dir, "IndiePulse: write, schedule and cross-post");
            Shortcut.Create(DesktopLink, home.Exe, "", home.Dir, "IndiePulse: write, schedule and cross-post");
            using (RegistryKey k = Registry.CurrentUser.CreateSubKey(UninstallKey))
            {
                k.SetValue("DisplayName", "IndiePulse");
                k.SetValue("DisplayVersion", home.Version);
                k.SetValue("DisplayIcon", home.Exe + ",0");
                k.SetValue("Publisher", Publisher);
                k.SetValue("URLInfoAbout", Website);
                k.SetValue("InstallLocation", home.Dir);
                k.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"));
                k.SetValue("UninstallString", "\"" + home.Exe + "\" --uninstall");
                k.SetValue("QuietUninstallString", "\"" + home.Exe + "\" --uninstall --quiet");
                k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                k.SetValue("EstimatedSize", (int)Math.Min(int.MaxValue, Files.Size(home.Dir) / 1024), RegistryValueKind.DWord);
            }
            ctx.Log.Write("shortcuts " + StartMenuLink + ", " + DesktopLink + "; uninstall entry HKCU\\" + UninstallKey);
        }

        // ── uninstall ────────────────────────────────────────────────────────
        // Quit (the tray finishes a publish first, closes its window), then whatever of this folder still runs
        public static void Stop(Home home, StepContext ctx)
        {
            ctx.Progress(-1);
            if (Instance.IsRunning(home))
            {
                ctx.Detail("Waiting for IndiePulse to quit (a post being published is finished first)…");
                if (!Instance.Quit(home, TimeSpan.FromMinutes(3))) ctx.Log.Write("the tray didn't quit in 3 minutes");
            }
            KillLeftovers(home, ctx.Log);
        }

        // Processes still running from this folder (its Python, uv) or with its window's browser profile
        public static void KillLeftovers(Home home, Log log)
        {
            string profile = home.BrowserProfile;
            const string query = "SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process WHERE Name='python.exe' " +
                "OR Name='pythonw.exe' OR Name='uv.exe' OR Name='msedge.exe' OR Name='chrome.exe' OR Name='IndiePulse.exe'";
            int me = Process.GetCurrentProcess().Id;
            try
            {
                using (var searcher = new ManagementObjectSearcher(query))
                using (ManagementObjectCollection found = searcher.Get())
                {
                    foreach (ManagementBaseObject o in found)
                    {
                        int pid = Convert.ToInt32(o["ProcessId"]);
                        string exe = o["ExecutablePath"] as string ?? "", cmd = o["CommandLine"] as string ?? "";
                        bool ours = Home.Inside(exe, home.Dir) || cmd.IndexOf(profile, StringComparison.OrdinalIgnoreCase) >= 0;
                        if (!ours || pid == me) continue;
                        try
                        {
                            Process.GetProcessById(pid).Kill();
                            log.Write("stopped pid " + pid + " " + exe);
                        }
                        catch (Exception ex) { log.Write("pid " + pid, ex); }
                    }
                }
            }
            catch (ManagementException ex) { log.Write("listing processes", ex); }
            System.Threading.Thread.Sleep(500);
        }

        // Shortcuts, autostart and the uninstall entry - only those that point at this folder
        public static void Unregister(Home home, StepContext ctx)
        {
            foreach (string lnk in new[] { StartMenuLink, DesktopLink })
            {
                if (File.Exists(lnk) && Home.Inside(Shortcut.Target(lnk), home.Dir))
                {
                    File.Delete(lnk);
                    ctx.Log.Write("deleted " + lnk);
                }
            }
            using (RegistryKey run = Registry.CurrentUser.OpenSubKey(RunKey, true))
            {
                string value = run == null ? null : run.GetValue("IndiePulse") as string;
                if (value != null && value.IndexOf(home.Dir, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    run.DeleteValue("IndiePulse", false);
                    ctx.Log.Write("deleted the autostart entry");
                }
            }
            if (IsInstalledAt(home))
            {
                Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
                ctx.Log.Write("deleted HKCU\\" + UninstallKey);
            }
        }

        // The program folder. This exe runs from it and can't be deleted, but it can be moved: to %TEMP%, where the
        // next IndiePulse.exe to start (or Windows' disk cleanup) deletes it. The data folder stays if it is inside.
        public static void DeleteProgram(Home home, string keepData, StepContext ctx)
        {
            ctx.Progress(-1);
            string self = typeof(Installer).Assembly.Location;
            var keep = new List<string>();
            if (!string.IsNullOrEmpty(keepData)) keep.Add(keepData);
            Environment.CurrentDirectory = Folders.Temp;   // a working directory inside would keep the folder
            if (Home.Inside(self, home.Dir))
            {
                string moved = Path.Combine(Folders.Temp, "IndiePulse-uninstall-" + DateTime.Now.Ticks + ".exe");
                try { File.Move(self, moved); }
                catch (Exception ex)
                {
                    ctx.Log.Write("couldn't move " + self + " out", ex);
                    keep.Add(self);
                }
            }
            Files.DeleteTree(home.Dir, keep.ToArray());
            ctx.Log.Write("deleted " + home.Dir + (keep.Count > 0 ? " (kept " + string.Join(", ", keep) + ")" : ""));
        }

        // Uninstallers moved to %TEMP% by earlier uninstalls (DeleteProgram)
        public static void CleanTemp()
        {
            try
            {
                foreach (string f in Directory.GetFiles(Folders.Temp, "IndiePulse-uninstall-*.exe"))
                {
                    try { File.Delete(f); }
                    catch (Exception) { }
                }
            }
            catch (Exception) { }
        }
    }
}
