using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace IndiePulse
{
    // The one window IndiePulse.exe shows: the choice of the first run (Install or Portable), the progress of whatever
    // it does (steps, a bar, the current line; a failure in place with Retry and Open log), the uninstaller's
    // question and its result. Laid out in code at 96 dpi and scaled to the screen's.
    sealed class SetupForm : Form
    {
        static readonly Color Ink = Color.FromArgb(0x1E, 0x1E, 0x1E);
        static readonly Color Muted = Color.FromArgb(0x5F, 0x6B, 0x7A);
        static readonly Color Faint = Color.FromArgb(0xA0, 0xA8, 0xB3);
        static readonly Color Accent = Color.FromArgb(0x25, 0x63, 0xEB);
        static readonly Color AccentSoft = Color.FromArgb(0xEF, 0xF5, 0xFF);
        static readonly Color Good = Color.FromArgb(0x16, 0xA3, 0x4A);
        static readonly Color Bad = Color.FromArgb(0xDC, 0x26, 0x26);
        static readonly Color Line = Color.FromArgb(0xE3, 0xE6, 0xEA);
        static readonly Color FooterBack = Color.FromArgb(0xF4, 0xF5, 0xF7);

        const int W = 600, H = 460, HeaderH = 84, FooterH = 60, Pad = 28;

        public readonly Log Log;
        readonly float k;
        readonly Panel body;
        readonly Button primary, secondary;
        readonly LinkLabel openLog;
        readonly Label title, subtitle;
        Action onPrimary, onSecondary;
        string logToOpen;
        volatile bool busy;
        StepContext running;

        public SetupForm(Log log, string windowTitle, string heading, string sub)
        {
            Log = log;
            using (Graphics g = CreateGraphics()) k = g.DpiX / 96f;
            SuspendLayout();
            AutoScaleMode = AutoScaleMode.None;
            Font = SystemFonts.MessageBoxFont;
            Text = windowTitle;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(S(W), S(H));
            BackColor = Color.White;
            ForeColor = Ink;
            Icon = AppIcon.Small;

            var header = new Panel { Bounds = new Rectangle(0, 0, S(W), S(HeaderH)), BackColor = Color.White };
            var logo = new PictureBox
            {
                Image = AppIcon.Large, SizeMode = PictureBoxSizeMode.Zoom,
                Bounds = new Rectangle(S(Pad), S(18), S(48), S(48))
            };
            title = new Label
            {
                Text = heading, AutoSize = false, Font = new Font("Segoe UI Semibold", 15f), ForeColor = Ink,
                Bounds = new Rectangle(S(92), S(14), S(W - 92 - Pad), S(34))
            };
            subtitle = new Label
            {
                Text = sub, AutoSize = false, ForeColor = Muted, AutoEllipsis = true,
                Bounds = new Rectangle(S(92), S(48), S(W - 92 - Pad), S(22))
            };
            header.Controls.AddRange(new Control[] { logo, title, subtitle });
            var rule = new Panel { Bounds = new Rectangle(0, S(HeaderH), S(W), Math.Max(1, S(1))), BackColor = Line };

            var footer = new Panel { Bounds = new Rectangle(0, S(H - FooterH), S(W), S(FooterH)), BackColor = FooterBack };
            footer.Paint += delegate(object s, PaintEventArgs e)
            {
                using (var pen = new Pen(Line)) e.Graphics.DrawLine(pen, 0, 0, footer.Width, 0);
            };
            primary = MakeButton(S(W - Pad - 104));
            secondary = MakeButton(S(W - Pad - 104 - 8 - 104));
            primary.Click += delegate { if (onPrimary != null) onPrimary(); };
            secondary.Click += delegate { if (onSecondary != null) onSecondary(); };
            openLog = new LinkLabel
            {
                Text = "Open log", AutoSize = true, Visible = false, LinkColor = Accent, ActiveLinkColor = Accent,
                Location = new Point(S(Pad), S(21))
            };
            openLog.LinkClicked += delegate { OpenLog(); };
            footer.Controls.AddRange(new Control[] { primary, secondary, openLog });

            body = new Panel { Bounds = new Rectangle(0, S(HeaderH) + 1, S(W), S(H - HeaderH - FooterH) - 1), BackColor = Color.White };
            Controls.AddRange(new Control[] { header, rule, body, footer });
            ResumeLayout(false);
        }

        int S(int px) { return (int)Math.Round(px * k); }

        Button MakeButton(int x)
        {
            var b = new Button
            {
                Bounds = new Rectangle(x, S(14), S(104), S(32)), FlatStyle = FlatStyle.System, Visible = false,
                UseVisualStyleBackColor = true
            };
            return b;
        }

        void Buttons(string first, Action a, string second, Action b)
        {
            primary.Text = first ?? "";
            primary.Visible = first != null;
            primary.Enabled = true;
            onPrimary = a;
            secondary.Text = second ?? "";
            secondary.Visible = second != null;
            secondary.Enabled = true;
            onSecondary = b;
            AcceptButton = first != null ? primary : null;
            if (first != null) primary.Focus();
        }

        void Clear()
        {
            body.SuspendLayout();
            foreach (Control c in body.Controls) c.Dispose();
            body.Controls.Clear();
        }

        Label AddLabel(string text, int x, int y, int w, int h, Color color, Font font)
        {
            var l = new Label
            {
                Text = text, AutoSize = false, ForeColor = color, Bounds = new Rectangle(S(x), S(y), S(w), S(h)),
                BackColor = Color.Transparent
            };
            if (font != null) l.Font = font;
            body.Controls.Add(l);
            return l;
        }

        Font Semibold(float size) { return new Font("Segoe UI Semibold", size); }

        public void SetHeading(string heading, string sub)
        {
            title.Text = heading;
            if (sub != null) subtitle.Text = sub;
        }

        // ── page 1: Install or Portable ──────────────────────────────────────
        public void ShowChoice(string question, string installTitle, string installText, string portableTitle,
            string portableText, bool portableFirst, string note, Action<bool> chosen)
        {
            Clear();
            AddLabel(question, Pad, 18, W - 2 * Pad, 26, Ink, Semibold(11f));
            var install = new OptionCard(this, installTitle, installText, new Rectangle(S(Pad), S(52), S(W - 2 * Pad), S(104)), k);
            var portable = new OptionCard(this, portableTitle, portableText, new Rectangle(S(Pad), S(166), S(W - 2 * Pad), S(104)), k);
            install.Other = portable;
            portable.Other = install;
            body.Controls.Add(install);
            body.Controls.Add(portable);
            AddLabel(note, Pad, 280, W - 2 * Pad, 34, Muted, null);
            body.ResumeLayout(false);
            Action label = null;
            label = delegate { primary.Text = install.Checked ? "Install" : "Start"; };
            install.Changed += label;
            portable.Changed += label;
            (portableFirst ? portable : install).Checked = true;
            Buttons(portableFirst ? "Start" : "Install", delegate { chosen(install.Checked); }, "Cancel", Close);
            CancelButton = secondary;
        }

        // ── the uninstaller's question ───────────────────────────────────────
        public void ShowConfirm(string question, string text, string checkText, Action<bool> confirmed)
        {
            Clear();
            AddLabel(question, Pad, 18, W - 2 * Pad, 26, Ink, Semibold(11f));
            AddLabel(text, Pad, 52, W - 2 * Pad, 96, Muted, null);
            var check = new CheckBox
            {
                Text = checkText, AutoSize = false, Bounds = new Rectangle(S(Pad), S(158), S(W - 2 * Pad), S(54)),
                CheckAlign = ContentAlignment.TopLeft, TextAlign = ContentAlignment.TopLeft
            };
            body.Controls.Add(check);
            body.ResumeLayout(false);
            Buttons("Uninstall", delegate { confirmed(check.Checked); }, "Cancel", Close);
            CancelButton = secondary;
        }

        // ── the end: a message and Close ─────────────────────────────────────
        public void ShowDone(string heading, string text)
        {
            Clear();
            AddLabel(heading, Pad, 18, W - 2 * Pad, 26, Ink, Semibold(11f));
            AddLabel(text, Pad, 52, W - 2 * Pad, 200, Muted, null);
            body.ResumeLayout(false);
            openLog.Visible = false;
            Buttons("Close", Close, null, null);
        }

        // ── progress ─────────────────────────────────────────────────────────
        // Runs `steps` one after the other on a worker thread; `done` (on this thread) after the last one. A failure
        // stays in the window: Retry goes on from the step that failed.
        public void Run(string heading, List<Step> steps, Action done)
        {
            Clear();
            AddLabel(heading, Pad, 18, W - 2 * Pad, 26, Ink, Semibold(11f));
            var glyphs = new List<Label>();
            var names = new List<Label>();
            int rowH = steps.Count > 7 ? 22 : 26;
            for (int i = 0; i < steps.Count; i++)
            {
                int y = 54 + i * rowH;
                glyphs.Add(AddLabel("○", Pad, y, 22, rowH, Faint, new Font("Segoe UI Symbol", 10f)));
                names.Add(AddLabel(steps[i].Title, Pad + 24, y + 1, W - 2 * Pad - 24, rowH, Faint, null));
            }
            var bar = new ProgressBar
            {
                Bounds = new Rectangle(S(Pad), S(260), S(W - 2 * Pad), S(10)), Style = ProgressBarStyle.Marquee,
                MarqueeAnimationSpeed = 30
            };
            body.Controls.Add(bar);
            Label detail = AddLabel("", Pad, 278, W - 2 * Pad, 22, Muted, null);
            detail.AutoEllipsis = true;
            Label error = AddLabel("", Pad, 254, W - 2 * Pad, 60, Bad, null);
            error.Visible = false;
            body.ResumeLayout(false);
            openLog.Visible = false;
            Buttons(null, null, "Cancel", Close);
            CancelButton = secondary;

            Action<int> start = null;
            start = delegate(int from)
            {
                busy = true;
                error.Visible = false;
                bar.Visible = detail.Visible = true;
                openLog.Visible = false;
                Buttons(null, null, "Cancel", Close);
                var thread = new Thread(delegate()
                {
                    for (int i = from; i < steps.Count; i++)
                    {
                        int at = i;
                        var ctx = new StepContext { Log = Log };
                        ctx.Detail = delegate(string text) { Ui(delegate { detail.Text = text; }); };
                        ctx.Progress = delegate(int pct)
                        {
                            Ui(delegate
                            {
                                if (pct < 0) bar.Style = ProgressBarStyle.Marquee;
                                else
                                {
                                    bar.Style = ProgressBarStyle.Continuous;
                                    bar.Value = Math.Max(0, Math.Min(100, pct));
                                }
                            });
                        };
                        running = ctx;
                        Ui(delegate
                        {
                            Mark(glyphs[at], names[at], "›", Accent, true);
                            detail.Text = "";
                            bar.Style = ProgressBarStyle.Marquee;
                        });
                        Log.Write("step: " + steps[at].Title);
                        try
                        {
                            steps[at].Run(ctx);
                        }
                        catch (Exception ex)
                        {
                            string message = ex is StepFailed ? ex.Message : "Something went wrong: " + ex.Message;
                            Log.Write("failed: " + steps[at].Title, ex);
                            busy = false;
                            Ui(delegate
                            {
                                Mark(glyphs[at], names[at], "✕", Bad, true);
                                bar.Visible = detail.Visible = false;
                                error.Text = message;
                                error.Visible = true;
                                logToOpen = ctx.LogToOpen;
                                openLog.Visible = true;
                                Buttons("Retry", delegate { Mark(glyphs[at], names[at], "○", Faint, false); start(at); },
                                    "Close", Close);
                            });
                            return;
                        }
                        Ui(delegate { Mark(glyphs[at], names[at], "✓", Good, false); });
                    }
                    busy = false;
                    Ui(delegate
                    {
                        bar.Style = ProgressBarStyle.Continuous;
                        bar.Value = 100;
                        if (done != null) done();
                    });
                });
                thread.IsBackground = true;
                thread.Start();
            };
            start(0);
        }

        void Mark(Label glyph, Label name, string sign, Color color, bool current)
        {
            glyph.Text = sign;
            glyph.ForeColor = color;
            name.ForeColor = color == Faint ? Faint : (color == Bad ? Bad : Ink);
            name.Font = current ? new Font(Font, FontStyle.Bold) : Font;
        }

        void Ui(Action a)
        {
            if (IsDisposed) return;
            try { BeginInvoke(a); }
            catch (InvalidOperationException) { }
        }

        // Close after a moment (the app's window is opening)
        public void CloseSoon(int ms)
        {
            var t = new System.Windows.Forms.Timer { Interval = ms };
            t.Tick += delegate { t.Stop(); Close(); };
            t.Start();
        }

        void OpenLog()
        {
            string file = logToOpen != null && File.Exists(logToOpen) ? logToOpen : Log.Path;
            try
            {
                if (file != null && File.Exists(file)) Process.Start(new ProcessStartInfo("notepad.exe", "\"" + file + "\""));
            }
            catch (Exception ex) { Log.Write("open log", ex); }
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (busy && e.CloseReason == CloseReason.UserClosing)
            {
                if (MessageBox.Show(this, "Stop setting up IndiePulse? You can run it again any time.", Text,
                        MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
                StepContext ctx = running;
                Process child = ctx == null ? null : ctx.Child;
                if (child != null)
                {
                    try { child.Kill(); }
                    catch (Exception) { }
                }
                Log.Write("cancelled by the user");
            }
            base.OnFormClosing(e);
        }

        // One of the two choices: a card with a radio button, its title and what it means
        sealed class OptionCard : Panel
        {
            readonly RadioButton radio;
            public OptionCard Other;
            public event Action Changed;

            public OptionCard(SetupForm form, string heading, string text, Rectangle bounds, float k)
            {
                Bounds = bounds;
                BackColor = Color.White;
                Cursor = Cursors.Hand;
                DoubleBuffered = true;
                int pad = (int)(14 * k);
                radio = new RadioButton
                {
                    Text = heading, AutoSize = false, Font = new Font("Segoe UI Semibold", 10f), ForeColor = Ink,
                    Bounds = new Rectangle(pad, (int)(10 * k), bounds.Width - 2 * pad, (int)(26 * k)), BackColor = Color.Transparent
                };
                var label = new Label
                {
                    Text = text, AutoSize = false, ForeColor = Muted, BackColor = Color.Transparent,
                    Bounds = new Rectangle(pad + (int)(19 * k), (int)(38 * k), bounds.Width - 2 * pad - (int)(19 * k), bounds.Height - (int)(44 * k))
                };
                Controls.Add(radio);
                Controls.Add(label);
                radio.CheckedChanged += delegate
                {
                    if (radio.Checked && Other != null) Other.Checked = false;
                    BackColor = radio.Checked ? AccentSoft : Color.White;
                    Invalidate();
                    if (Changed != null) Changed();
                };
                EventHandler pick = delegate { Checked = true; radio.Focus(); };
                Click += pick;
                label.Click += pick;
                label.Cursor = Cursors.Hand;
            }

            public bool Checked
            {
                get { return radio.Checked; }
                set { radio.Checked = value; }
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                base.OnPaint(e);
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                var r = new Rectangle(0, 0, Width - 1, Height - 1);
                using (var pen = new Pen(radio.Checked ? Accent : Line, radio.Checked ? 2f : 1f))
                    e.Graphics.DrawRectangle(pen, radio.Checked ? new Rectangle(1, 1, Width - 3, Height - 3) : r);
            }
        }
    }

    // The app icon, from the exe's resources (app/static/indiepulse.ico, built in by tools/build_launcher.ps1)
    static class AppIcon
    {
        static Icon Load(int size)
        {
            using (Stream s = typeof(AppIcon).Assembly.GetManifestResourceStream("IndiePulse.ico"))
                return s == null ? SystemIcons.Application : new Icon(s, size, size);
        }

        public static Icon Small { get { return Load(32); } }

        public static Image Large
        {
            get
            {
                using (Icon icon = Load(256)) return icon.ToBitmap();
            }
        }
    }
}
