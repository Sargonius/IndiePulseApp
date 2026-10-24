using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace IndiePulse
{
    // Talking to the running IndiePulse of a folder: named events its tray creates (app/desktop/winapi.py,
    // InstanceEvents) - "show" exists while the tray runs, setting it brings up the window; "quit" makes it quit -
    // and "ready", which this exe creates before it starts a tray and the tray sets once its server answers. While an
    // update is being installed, apply_update.py holds the "updating" mutex.
    static class Instance
    {
        [DllImport("user32.dll")]
        static extern bool AllowSetForegroundWindow(int processId);

        const int AnyProcess = -1;

        static string Name(Home home, string what)
        {
            return "Local\\IndiePulse-" + home.Key + "-" + what;
        }

        static EventWaitHandle Open(Home home, string what)
        {
            EventWaitHandle e;
            try { return EventWaitHandle.TryOpenExisting(Name(home, what), out e) ? e : null; }
            catch (UnauthorizedAccessException) { return null; }
        }

        public static bool IsRunning(Home home)
        {
            EventWaitHandle e = Open(home, "show");
            if (e == null) return false;
            e.Dispose();
            return true;
        }

        // The window to the front (the tray may take the foreground only because this process, the one the user
        // just started, allows it)
        public static bool Show(Home home)
        {
            using (EventWaitHandle e = Open(home, "show"))
            {
                if (e == null) return false;
                AllowSetForegroundWindow(AnyProcess);
                return e.Set();
            }
        }

        // Quit, and wait for the tray to be gone (the worker finishes a publish in progress first)
        public static bool Quit(Home home, TimeSpan timeout)
        {
            using (EventWaitHandle e = Open(home, "quit"))
            {
                if (e == null) return !IsRunning(home);
                e.Set();
            }
            var clock = Stopwatch.StartNew();
            while (clock.Elapsed < timeout)
            {
                if (!IsRunning(home)) return true;
                Thread.Sleep(300);
            }
            return !IsRunning(home);
        }

        public static EventWaitHandle CreateReady(Home home)
        {
            var e = new EventWaitHandle(false, EventResetMode.ManualReset, Name(home, "ready"));
            e.Reset();  // one left set by an earlier start that nobody waited for
            return e;
        }

        public static bool UpdateRunning(Home home)
        {
            Mutex m;
            try
            {
                if (!Mutex.TryOpenExisting(Name(home, "updating"), out m)) return false;
            }
            catch (UnauthorizedAccessException) { return true; }
            m.Dispose();
            return true;
        }

        // Until apply_update.py has finished (it starts the new tray itself); false: still running after `timeout`
        public static bool WaitForUpdate(Home home, TimeSpan timeout)
        {
            Mutex m;
            try
            {
                if (!Mutex.TryOpenExisting(Name(home, "updating"), out m)) return true;
            }
            catch (UnauthorizedAccessException) { return false; }
            using (m)
            {
                bool got;
                try { got = m.WaitOne(timeout); }
                catch (AbandonedMutexException) { got = true; }
                if (got) m.ReleaseMutex();
                return got;
            }
        }
    }

    // setup.log in the data folder's logs\: what the setup, a repair and the uninstaller did, with uv's output.
    // Lines written before the data folder is known (the choice in the setup window decides it) are kept and
    // written once it is.
    sealed class Log
    {
        readonly object sync = new object();
        readonly StringBuilder pending = new StringBuilder();
        string path;

        public string Path { get { return path; } }

        public void Open(string file)
        {
            lock (sync)
            {
                try
                {
                    Directory.CreateDirectory(System.IO.Path.GetDirectoryName(file));
                    if (File.Exists(file) && new FileInfo(file).Length > 2 * 1024 * 1024)
                    {
                        File.Copy(file, file + ".old", true);   // one previous, not an ever-growing file
                        File.Delete(file);
                    }
                    File.AppendAllText(file, pending.ToString(), Encoding.UTF8);
                    pending.Length = 0;
                    path = file;
                }
                catch (Exception)
                {
                    // no log is no reason not to start
                }
            }
        }

        public void Write(string text)
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " | " + text + Environment.NewLine;
            lock (sync)
            {
                if (path == null) { pending.Append(line); return; }
                try { File.AppendAllText(path, line, Encoding.UTF8); }
                catch (Exception) { }
            }
        }

        public void Write(string what, Exception ex)
        {
            Write(what + ": " + ex);
        }
    }
}
