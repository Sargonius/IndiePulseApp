using System;
using System.Diagnostics;

namespace IndiePulse
{
    // One line of the setup window's list: what it says, and the work behind it (run on a worker thread)
    sealed class Step
    {
        public readonly string Id;
        public readonly string Title;
        public readonly Action<StepContext> Run;

        public Step(string id, string title, Action<StepContext> run)
        {
            Id = id;
            Title = title;
            Run = run;
        }
    }

    // What a running step can tell the window
    sealed class StepContext
    {
        public Log Log;
        // a line under the progress bar
        public Action<string> Detail = delegate { };
        // 0..100; -1: busy, no measure
        public Action<int> Progress = delegate { };
        // what "Open log" opens when this step fails (setup.log otherwise)
        public string LogToOpen;
        // a tool that is running, stopped if the window is closed meanwhile
        public volatile Process Child;
    }

    // A failure to show the user as it is (with Retry and Open log next to it)
    sealed class StepFailed : Exception
    {
        public StepFailed(string message) : base(message) { }
        public StepFailed(string message, Exception inner) : base(message, inner) { }
    }
}
