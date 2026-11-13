// RoomCast.exe - a thin launcher around "Run RoomCast.bat".
//
// It does not reimplement any setup logic; the batch file remains the single
// source of truth. The exe only decides whether the batch window is shown:
//   - When packages are about to be installed (first run, or requirements.txt
//     changed): the window is shown so the user can watch the install and read
//     any error message / "pause" prompt.
//   - Otherwise: the window is hidden, so the app opens with no console flash.
//
// Build with launcher\build.bat (uses the C# compiler included with Windows).

using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Program
{
    [STAThread]
    static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string bat = Path.Combine(root, "Run RoomCast.bat");

        if (!File.Exists(bat))
        {
            MessageBox.Show(
                "Could not find \"Run RoomCast.bat\" next to RoomCast.exe.\n\n" +
                "Keep RoomCast.exe in the RoomCast folder.",
                "RoomCast", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        bool installing = NeedsInstall(root);

        var psi = new ProcessStartInfo(bat)
        {
            WorkingDirectory = root,
            UseShellExecute = true,
            WindowStyle = installing ? ProcessWindowStyle.Normal : ProcessWindowStyle.Hidden,
        };

        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            MessageBox.Show("Could not start RoomCast:\n\n" + e.Message,
                "RoomCast", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }

    // Mirrors the batch file's check: Python and the packages are set up on first run
    // (in "runtime" or ".venv"), and packages are reinstalled whenever requirements.txt
    // differs from the copy saved after the last install.
    static bool NeedsInstall(string root)
    {
        try
        {
            string env;
            if (File.Exists(Path.Combine(root, @"runtime\python.exe"))) env = "runtime";
            else if (File.Exists(Path.Combine(root, @".venv\Scripts\python.exe"))) env = ".venv";
            else return true;
            string installed = Path.Combine(root, env, "requirements.installed");
            if (!File.Exists(installed)) return true;
            byte[] a = File.ReadAllBytes(Path.Combine(root, "requirements.txt"));
            byte[] b = File.ReadAllBytes(installed);
            if (a.Length != b.Length) return true;
            for (int i = 0; i < a.Length; i++) if (a[i] != b[i]) return true;
            return false;
        }
        catch (IOException) { return true; }
    }
}
