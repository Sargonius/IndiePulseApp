using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace IndiePulse
{
    // IndiePulse.exe, the one thing a user starts.
    //
    //   IndiePulse.exe                  the first time: the setup (Install for this user, or Portable); then: start
    //                                   IndiePulse, or bring up its window when it runs
    //   IndiePulse.exe --background     start without opening the window (autostart; nothing when it runs)
    //   IndiePulse.exe --install        the setup without the question: Install (--portable: Portable)
    //   IndiePulse.exe --uninstall      what Settings -> Apps runs: asks, then removes it (--quiet: no questions,
    //                                   no window; --delete-data: the data too)
    //   IndiePulse.exe --paths <file>   writes what it found (folders, layout, the runtime's state) as JSON
    //
    // Starting means: the venv in order (Runtime.cs; a window with the progress only when something has to be
    // done), then `pythonw -m app.desktop.tray`, which runs the server and the worker and opens the app window.
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Options o = Options.Parse(args);
            Home home = Home.OfThisExe();
            if (o.PathsFile != null) return Diagnostics.Write(home, o.PathsFile);
            var log = new Log();
            try
            {
                Installer.CleanTemp();
                if (!home.HasCode)
                {
                    NoCode(home);
                    return 1;
                }
                if (o.Uninstall) return Flows.Uninstall(home, o, log);
                return Flows.Launch(home, o, log);
            }
            catch (Exception ex)
            {
                log.Write("fatal", ex);
                if (log.Path == null) log.Open(Path.Combine(Folders.Temp, "IndiePulse-setup.log"));
                MessageBox.Show("IndiePulse couldn't start:\n\n" + ex.Message + "\n\nDetails: " + log.Path, "IndiePulse",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        // Started from inside the zip (Explorer unpacks only the exe into a temp folder), or lib\ is gone
        static void NoCode(Home home)
        {
            bool fromZip = Home.Inside(home.Dir, Folders.Temp);
            string text = fromZip
                ? "IndiePulse.exe was started from inside the zip.\n\nExtract the zip first (right-click it → Extract All…), " +
                  "then start IndiePulse.exe in the folder you get."
                : "IndiePulse's files are missing next to IndiePulse.exe (the lib folder in " + home.Dir + ").\n\n" +
                  "Extract the whole zip again, then start IndiePulse.exe.";
            MessageBox.Show(text, "IndiePulse", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    sealed class Options
    {
        public bool Background, Uninstall, Quiet, DeleteData;
        public string Choice;      // "install" | "portable" | null: ask
        public string PathsFile;
        public string Raw = "";    // to hand over to the installed copy as they came

        public static Options Parse(string[] args)
        {
            var o = new Options();
            var raw = new List<string>();
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i].ToLowerInvariant();
                raw.Add(args[i].IndexOf(' ') >= 0 ? "\"" + args[i] + "\"" : args[i]);
                if (a == "--background") o.Background = true;
                else if (a == "--uninstall") o.Uninstall = true;
                else if (a == "--quiet") o.Quiet = true;
                else if (a == "--delete-data") o.DeleteData = true;
                else if (a == "--install") o.Choice = "install";
                else if (a == "--portable") o.Choice = "portable";
                else if (a == "--paths" && i + 1 < args.Length) o.PathsFile = args[++i];
            }
            o.Raw = string.Join(" ", raw);
            return o;
        }
    }

    static class Flows
    {
        const string Tagline = "write, schedule and cross-post your game's news";

        static bool SetUp(Home home)
        {
            // a checkout is always "set up" (its runtime gets prepared like a repair); a release once it is
            // portable or installed
            return !home.Release || File.Exists(home.PortableMarker) || Installer.IsInstalledAt(home);
        }

        public static int Launch(Home home, Options o, Log log)
        {
            if (!SetUp(home))
            {
                Home installed = Installer.Installed();
                if (o.Choice == null && installed != null && !Home.SamePath(installed.Dir, home.Dir)
                    && Versions.Compare(home.Version, installed.Version) <= 0)
                {
                    // the downloaded copy started again, the installed one is as new: that one, as if it was started
                    Process.Start(new ProcessStartInfo(installed.Exe, o.Raw) { UseShellExecute = false, WorkingDirectory = installed.Dir });
                    return 0;
                }
                return Setup(home, installed, o, log);
            }
            return Start(home, o, log);
        }

        // ── a home that is set up ────────────────────────────────────────────
        static int Start(Home home, Options o, Log log)
        {
            log.Open(Path.Combine(home.Logs, "setup.log"));
            var steps = new List<Step>();
            if (Instance.UpdateRunning(home))
            {
                // apply_update.py is swapping the code and starts the new version itself; then it runs
                steps.Add(new Step("update", "Waiting for the update to finish", delegate(StepContext ctx)
                {
                    ctx.Detail("IndiePulse is installing an update; it starts again by itself.");
                    if (!Instance.WaitForUpdate(home, TimeSpan.FromMinutes(10)))
                        throw new StepFailed("The update is taking long. Its log: logs\\update.log in " + home.Data + ".");
                }));
            }
            else if (Instance.IsRunning(home))
            {
                if (!o.Background) Instance.Show(home);
                return 0;
            }
            var rt = new Runtime(home);
            if (steps.Count == 0)
            {
                List<Step> plan = rt.Plan();
                if (plan.Count == 0)
                {
                    Process p = rt.StartTray(o.Background);
                    if (!p.WaitForExit(4000) || p.ExitCode == 0) return 0;
                    log.Write("the tray exited at once, code " + p.ExitCode);   // shown with its log below
                }
                steps.AddRange(plan);
            }
            steps.Add(new Step("start", "Starting IndiePulse", delegate(StepContext ctx)
            {
                if (Instance.IsRunning(home))
                {
                    if (!o.Background) Instance.Show(home);
                    return;
                }
                rt.StartAndWait(ctx, o.Background);
            }));
            var form = new SetupForm(log, "IndiePulse", "IndiePulse", "Version " + home.Version + " · " + Tagline);
            bool ok = false;
            form.Shown += delegate
            {
                form.Run(steps.Count > 1 ? "Getting IndiePulse ready" : "Starting IndiePulse", steps, delegate
                {
                    ok = true;
                    form.CloseSoon(900);
                });
            };
            Application.Run(form);
            return ok ? 0 : 1;
        }

        // ── the first run of a release: Install or Portable ──────────────────
        static int Setup(Home home, Home installed, Options o, Log log)
        {
            bool update = installed != null && !Home.SamePath(installed.Dir, home.Dir);
            bool carried = home.KitPresent || Drives.Removable(home.Dir);
            var form = new SetupForm(log, "IndiePulse Setup", "Set up IndiePulse", "Version " + home.Version + " · " + Tagline);
            bool ok = false;
            Action finished = delegate
            {
                ok = true;
                form.SetHeading("IndiePulse is ready", null);
                form.CloseSoon(1200);
            };
            Action<bool> go = delegate(bool install)
            {
                if (install) RunInstall(form, home, finished);
                else RunPortable(form, home, finished);
            };
            if (o.Choice != null)
            {
                form.Shown += delegate { go(o.Choice == "install"); };
            }
            else
            {
                string installTitle = update
                    ? "Update IndiePulse on this computer (" + installed.Version + " → " + home.Version + ")"
                    : "Install for this user";
                string installText = update
                    ? "Replaces the installed IndiePulse in " + Short(installed.Dir) + ". Your posts, settings and " +
                      "media stay as they are."
                    : "Copies it to " + Short(Folders.InstallDir) + " (no admin rights needed), with Start-menu and " +
                      "desktop shortcuts and an entry in Settings → Apps to remove it. Posts and settings: " +
                      Short(Path.Combine(Folders.LocalAppData, "IndiePulse")) + ".";
                string portableText = "Runs from " + Short(home.Dir) + " and writes nothing to this computer: no " +
                    "shortcuts, registry entries or autostart. Posts and settings stay in its data folder — good " +
                    "for a USB drive.";
                form.ShowChoice("How would you like to use IndiePulse?", installTitle, installText,
                    "Portable — run from this folder", portableText, carried && !update,
                    (home.KitPresent ? "ClubKit is on this drive: its Python and uv are used. " :
                        "The first start downloads Python and the packages IndiePulse needs (about 70 MB): a few " +
                        "minutes. ") + "Nothing else is installed on the computer.", go);
            }
            Application.Run(form);
            return ok ? 0 : 1;
        }

        // A path to show in a line of text: C:\Users\me\…\portable\IndiePulse when it is long
        static string Short(string path)
        {
            if (path.Length <= 56) return path;
            string root = Path.GetPathRoot(path);
            string[] parts = path.Substring(root.Length).Split('\\');
            if (parts.Length <= 4) return path;
            return root + parts[0] + "\\" + parts[1] + "\\…\\" + parts[parts.Length - 2] + "\\" + parts[parts.Length - 1];
        }

        static void RunInstall(SetupForm form, Home source, Action finished)
        {
            var target = new Home(Folders.InstallDir, true);
            form.Log.Open(Path.Combine(target.Logs, "setup.log"));
            form.Log.Write("install " + source.Version + " from " + source.Dir + " to " + target.Dir);
            var rt = new Runtime(target);
            var steps = new List<Step>();
            if (Instance.IsRunning(target))
                steps.Add(new Step("stop", "Closing the IndiePulse that is running", delegate(StepContext ctx) { Installer.Stop(target, ctx); }));
            steps.Add(new Step("copy", "Copying IndiePulse", delegate(StepContext ctx) { Installer.Copy(source, target, ctx); }));
            steps.AddRange(rt.AllSteps());
            steps.Add(new Step("register", "Adding shortcuts and an entry in Settings → Apps",
                delegate(StepContext ctx) { Installer.Register(target, ctx); }));
            steps.Add(new Step("start", "Starting IndiePulse", delegate(StepContext ctx) { rt.StartAndWait(ctx, false); }));
            form.Run("Installing IndiePulse", steps, finished);
        }

        static void RunPortable(SetupForm form, Home home, Action finished)
        {
            if (!File.Exists(home.PortableMarker))
            {
                File.WriteAllText(home.PortableMarker,
                    "IndiePulse runs portable from this folder: its data is in data\\ next to IndiePulse.exe, and nothing\r\n" +
                    "is written to the computer (no shortcuts, registry entries or autostart). Keep this file.\r\n",
                    Encoding.UTF8);
            }
            form.Log.Open(Path.Combine(home.Logs, "setup.log"));
            form.Log.Write("portable " + home.Version + " in " + home.Dir);
            var rt = new Runtime(home);
            var steps = new List<Step>(rt.AllSteps());
            steps.Add(new Step("start", "Starting IndiePulse", delegate(StepContext ctx) { rt.StartAndWait(ctx, false); }));
            form.Run("Getting IndiePulse ready", steps, finished);
        }

        // ── uninstall ────────────────────────────────────────────────────────
        // The data folder may go only when it is IndiePulse's own: never a drive, the profile, or the code
        static bool DataDeletable(string data, Home home)
        {
            string name = Path.GetFileName(data.TrimEnd('\\'));
            if (data.Length <= 3 || Home.SamePath(data, home.Root) || Home.SamePath(data, home.Dir)) return false;
            return string.Equals(name, "IndiePulse", StringComparison.OrdinalIgnoreCase)
                || (string.Equals(name, "data", StringComparison.OrdinalIgnoreCase) && Home.Inside(data, home.Dir));
        }

        public static int Uninstall(Home home, Options o, Log log)
        {
            if (!Installer.IsInstalledAt(home))
            {
                if (!o.Quiet)
                    MessageBox.Show("This IndiePulse isn't installed on this computer: it runs portable from " + home.Dir +
                        ".\n\nTo remove it, quit it (its icon in the notification area → Quit) and delete that folder.",
                        "IndiePulse", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 1;
            }
            string data = home.Data;
            // in %TEMP%: the program folder goes, and maybe the data folder
            log.Open(Path.Combine(Folders.Temp, "IndiePulse-uninstall.log"));
            log.Write("uninstall " + home.Version + " from " + home.Dir + "; data " + data);
            Func<bool, List<Step>> plan = delegate(bool deleteData)
            {
                var steps = new List<Step>
                {
                    new Step("stop", "Closing IndiePulse", delegate(StepContext ctx) { Installer.Stop(home, ctx); }),
                    new Step("unregister", "Removing its shortcuts and its entry in Settings → Apps",
                        delegate(StepContext ctx) { Installer.Unregister(home, ctx); }),
                    new Step("files", "Removing the program", delegate(StepContext ctx)
                    {
                        Installer.DeleteProgram(home, deleteData ? null : data, ctx);
                    }),
                };
                if (deleteData && DataDeletable(data, home))
                    steps.Add(new Step("data", "Deleting your data", delegate(StepContext ctx) { Files.DeleteTree(data); }));
                return steps;
            };
            if (o.Quiet) return Headless(plan(o.DeleteData), log) ? 0 : 1;

            var form = new SetupForm(log, "Uninstall IndiePulse", "Uninstall IndiePulse", "Version " + home.Version + " in " + home.Dir);
            bool ok = false;
            form.ShowConfirm("Remove IndiePulse from this computer?",
                "This removes the program from " + home.Dir + ", its shortcuts, its entry in Settings → Apps and its " +
                "autostart. IndiePulse is closed first; a post being published is finished.",
                "Also delete my posts, settings and media (" + data + ")",
                delegate(bool deleteData)
                {
                    form.Run("Uninstalling IndiePulse", plan(deleteData), delegate
                    {
                        ok = true;
                        form.ShowDone("IndiePulse has been removed", deleteData
                            ? "Your posts, settings and media were deleted too."
                            : "Your posts, settings and media are still in " + data + ": install IndiePulse again and " +
                              "they are there. Delete that folder to remove them as well.");
                    });
                });
            Application.Run(form);
            return ok ? 0 : 1;
        }

        static bool Headless(List<Step> steps, Log log)
        {
            foreach (Step s in steps)
            {
                log.Write("step: " + s.Title);
                try { s.Run(new StepContext { Log = log }); }
                catch (Exception ex)
                {
                    log.Write("failed: " + s.Title, ex);
                    return false;
                }
            }
            log.Write("done");
            return true;
        }
    }

    // --paths: what this exe makes of its folder, for tests/test_launcher.py and for a look when something is off
    static class Diagnostics
    {
        public static int Write(Home home, string file)
        {
            var rt = new Runtime(home);
            var plan = new List<string>();
            foreach (Step s in rt.Plan()) plan.Add(Json(s.Id));
            string stamp = "";
            try { stamp = home.RequirementsHash(); }
            catch (Exception) { }
            var pairs = new List<string>
            {
                Pair("home", Json(home.Dir)), Pair("root", Json(home.Root)), Pair("release", home.Release ? "true" : "false"),
                Pair("runtime", Json(home.Runtime)), Pair("venv", Json(home.Venv)), Pair("data", Json(home.Data)),
                Pair("kit", Json(home.Kit)), Pair("portable", home.Portable ? "true" : "false"),
                Pair("installed_here", Installer.IsInstalledAt(home) ? "true" : "false"),
                Pair("key", Json(home.Key)), Pair("version", Json(home.Version)),
                Pair("requirements_hash", Json(stamp)), Pair("venv_alive", home.VenvAlive() ? "true" : "false"),
                Pair("uv", Json(rt.Uv)), Pair("uv_cache", Json(rt.Cache)), Pair("plan", "[" + string.Join(", ", plan) + "]"),
                Pair("install_dir", Json(Folders.InstallDir)), Pair("desktop", Json(Folders.Desktop)),
                Pair("start_menu", Json(Folders.StartMenuPrograms)), Pair("removable", Drives.Removable(home.Dir) ? "true" : "false"),
            };
            File.WriteAllText(file, "{\n  " + string.Join(",\n  ", pairs) + "\n}\n", new UTF8Encoding(false));
            return 0;
        }

        static string Pair(string key, string value) { return Json(key) + ": " + value; }

        static string Json(string s)
        {
            var sb = new StringBuilder("\"");
            foreach (char c in s ?? "")
            {
                if (c == '"' || c == '\\') sb.Append('\\').Append(c);
                else if (c < ' ') sb.AppendFormat("\\u{0:x4}", (int)c);
                else sb.Append(c);
            }
            return sb.Append('"').ToString();
        }
    }
}
