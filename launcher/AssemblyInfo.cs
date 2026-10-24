using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

[assembly: AssemblyTitle("IndiePulse")]
[assembly: AssemblyDescription("IndiePulse: write, schedule and cross-post your game's news")]
[assembly: AssemblyProduct("IndiePulse")]
[assembly: AssemblyCompany("Evgeny Nesterovskiy")]
[assembly: AssemblyCopyright("Copyright (c) 2025-2026 Evgeny Nesterovskiy, MIT license")]
[assembly: ComVisible(false)]
// The version comes from app/__init__.py: tools/build_launcher.ps1 writes it into obj\Version.cs.
// csc doesn't add this by itself; without it .NET treats the exe as a 4.0 one, which (among other things) speaks
// only TLS 1.0 by default - GitHub wouldn't answer.
[assembly: TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]
