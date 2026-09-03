// RenPyHD.exe - minimal launcher / lanceur minimal : starts the Gradio app with the embedded Python of the
// DLSS 5 Visual Enhancer (DLSS5\). Built with csc.exe from the .NET Framework (see build_launcher.bat).
// Exit code 75 from Python = "restart me" (UI language change): the launcher relaunches the same command line.
using System;
using System.Diagnostics;
using System.IO;
using System.Collections.Generic;
using System.Windows.Forms;

static class Launcher
{
    const int RestartExitCode = 75;

    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string tool = Path.Combine(root, "DLSS5");
        string python = Path.Combine(tool, @"bin\python-3.13.15-embed-amd64\python.exe");
        string app = Path.Combine(root, @"app\renpy_hd_app.py");

        Console.Title = "RenPyHD";
        Console.WriteLine("RenPyHD - DLSS 5 Neural Rendering for Ren'Py games / pour les jeux Ren'Py");
        Console.WriteLine("Folder / Dossier : " + root);
        Console.WriteLine("Close this window to stop the application. / Fermez cette fenetre pour arreter l'application.");
        Console.WriteLine();

        foreach (string required in new[] { python, app, Path.Combine(tool, "app.py") })
        {
            if (!File.Exists(required))
            {
                string msg = "Missing file / Fichier manquant :\n" + required +
                             "\n\nRun setup.bat first: it downloads the DLSS 5 Visual Enhancer into DLSS5\\.\n" +
                             "Lancez d'abord setup.bat : il telecharge le DLSS 5 Visual Enhancer dans DLSS5\\.";
                Console.Error.WriteLine(msg);
                MessageBox.Show(msg, "RenPyHD", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 2;
            }
        }

        try
        {
            Console.OutputEncoding = System.Text.Encoding.UTF8;
        }
        catch { }

        int code;
        do
        {
            code = RunOnce(python, app, tool, args);
            if (code == RestartExitCode)
            {
                Console.WriteLine();
                Console.WriteLine("Restarting RenPyHD (language change) / Redemarrage de RenPyHD (changement de langue)...");
                Console.WriteLine();
            }
        } while (code == RestartExitCode);
        return code;
    }

    static int RunOnce(string python, string app, string tool, string[] args)
    {
        var psi = new ProcessStartInfo();
        psi.FileName = python;
        psi.Arguments = "\"" + app + "\" --tool \"" + tool + "\"" + (args.Length > 0 ? " " + string.Join(" ", args) : "");
        psi.WorkingDirectory = tool;
        psi.UseShellExecute = false;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
        psi.StandardErrorEncoding = System.Text.Encoding.UTF8;
        psi.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        psi.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        psi.EnvironmentVariables["GRADIO_ANALYTICS_ENABLED"] = "False";
        psi.EnvironmentVariables["HF_HUB_OFFLINE"] = "1";

        var tail = new Queue<string>();
        object tailLock = new object();
        DataReceivedEventHandler onLine = (sender, e) =>
        {
            if (e.Data == null) return;
            Console.WriteLine(e.Data);
            lock (tailLock)
            {
                tail.Enqueue(e.Data);
                while (tail.Count > 25) tail.Dequeue();
            }
        };

        Process proc;
        try
        {
            proc = Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show("Cannot start Python / Impossible de demarrer Python :\n" + ex.Message, "RenPyHD", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 3;
        }
        proc.OutputDataReceived += onLine;
        proc.ErrorDataReceived += onLine;
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        // If the user closes the console, Python is stopped too. / Si l'utilisateur ferme la console, on arrete Python aussi.
        EventHandler onExit = (s, e) => { try { if (!proc.HasExited) proc.Kill(); } catch { } };
        AppDomain.CurrentDomain.ProcessExit += onExit;
        proc.WaitForExit();
        AppDomain.CurrentDomain.ProcessExit -= onExit;

        if (proc.ExitCode != 0 && proc.ExitCode != RestartExitCode)
        {
            string details;
            lock (tailLock) { details = string.Join("\n", tail.ToArray()); }
            MessageBox.Show("RenPyHD stopped with code / s'est arrete avec le code " + proc.ExitCode + ".\n\nLast lines / Dernieres lignes :\n" + details,
                            "RenPyHD - error / erreur", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        return proc.ExitCode;
    }
}
