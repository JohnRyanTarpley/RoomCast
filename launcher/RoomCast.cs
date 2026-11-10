// RoomCast.exe - a thin launcher around "Run RoomCast.bat".
//
// It does not reimplement any setup logic; the batch file remains the single
// source of truth. The exe only decides whether the batch window is shown:
//   - First run (no .venv yet): the window is shown so the user can watch the
//     package install and read any error message / "pause" prompt.
//   - Later runs: the window is hidden, so the app opens with no console flash.
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

        bool firstRun = !File.Exists(Path.Combine(root, @".venv\Scripts\python.exe"));

        var psi = new ProcessStartInfo(bat)
        {
            WorkingDirectory = root,
            UseShellExecute = true,
            WindowStyle = firstRun ? ProcessWindowStyle.Normal : ProcessWindowStyle.Hidden,
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
}
