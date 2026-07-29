using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;

namespace VideoEditInstaller;

internal sealed record DownloadSpec(string Name, string Url, string Sha256);

internal sealed record InstallerOptions(string InstallRoot, bool SkipWhisper, bool DryRun);

internal static class Program
{
    private const string Version = "0.2.1";
    private const string ReleaseTag = "v0.2.1";
    private const string Repository = "https://github.com/klimPaskov/video-editor";
    private const string WhisperModel = "small";

    private static readonly DownloadSpec Uv = new(
        "uv",
        "https://github.com/astral-sh/uv/releases/download/0.12.0/uv-x86_64-pc-windows-msvc.zip",
        "68200e25de594df92387186bbfb9d9df606ec1d87efaa0ae0c7f690970e53db6"
    );

    private static readonly DownloadSpec Node = new(
        "Node.js",
        "https://nodejs.org/dist/v22.23.1/node-v22.23.1-win-x64.zip",
        "7df0bc9375723f4a86b3aa1b7cc73342423d9677a8df4538aca31a049e309c29"
    );

    private static readonly DownloadSpec Ffmpeg = new(
        "FFmpeg",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip",
        "cdcc03a71f50a0bdd9166aa47f3c56ad8b497d14e9f443485a062ec2933197aa"
    );

    private static readonly DownloadSpec Whisper = new(
        "Whisper model",
        "https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
        "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794"
    );

    public static async Task<int> Main(string[] args)
    {
        try
        {
            InstallerOptions options = ParseOptions(args);
            if (options.DryRun)
            {
                PrintPlan(options);
                return 0;
            }

            await InstallAsync(options);
            Console.WriteLine();
            Console.WriteLine("VideoEdit is ready.");
            Console.WriteLine($"Install folder: {options.InstallRoot}");
            Console.WriteLine($"Run: {Path.Combine(options.InstallRoot, "VideoEdit.cmd")}");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine();
            Console.Error.WriteLine($"Installation failed: {ex.Message}");
            Console.Error.WriteLine("Nothing in the source repository was changed.");
            return 1;
        }
    }

    private static InstallerOptions ParseOptions(string[] args)
    {
        string root = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "VideoEdit"
        );
        bool skipWhisper = false;
        bool dryRun = false;

        for (int index = 0; index < args.Length; index++)
        {
            switch (args[index].ToLowerInvariant())
            {
                case "--install-dir":
                    if (++index >= args.Length)
                    {
                        throw new ArgumentException("--install-dir requires a path.");
                    }
                    root = Path.GetFullPath(args[index]);
                    break;
                case "--skip-whisper":
                    skipWhisper = true;
                    break;
                case "--dry-run":
                    dryRun = true;
                    break;
                case "--help":
                case "-h":
                    PrintUsage();
                    Environment.Exit(0);
                    break;
                default:
                    throw new ArgumentException($"Unknown option: {args[index]}");
            }
        }

        return new InstallerOptions(root, skipWhisper, dryRun);
    }

    private static void PrintUsage()
    {
        Console.WriteLine("VideoEdit Installer");
        Console.WriteLine();
        Console.WriteLine("Downloads the pinned local runtime, media tools, application, and Whisper model.");
        Console.WriteLine();
        Console.WriteLine("Options:");
        Console.WriteLine("  --install-dir PATH  Install somewhere other than %LOCALAPPDATA%\\VideoEdit");
        Console.WriteLine("  --skip-whisper      Install dependencies without the model download");
        Console.WriteLine("  --dry-run           Show the download plan without changing the machine");
    }

    private static void PrintPlan(InstallerOptions options)
    {
        Console.WriteLine($"VideoEdit {Version} installer plan");
        Console.WriteLine($"Install folder: {options.InstallRoot}");
        Console.WriteLine($"Application: {Repository}/archive/refs/tags/{ReleaseTag}.zip");
        Console.WriteLine($"Download: {Uv.Name} 0.12.0");
        Console.WriteLine($"Download: {Node.Name} 22.23.1");
        Console.WriteLine($"Download: {Ffmpeg.Name} LGPL build");
        if (!options.SkipWhisper)
        {
            Console.WriteLine($"Download: {WhisperModel} Whisper model");
        }
        Console.WriteLine("Install: managed Python 3.11, Python dependencies, and Remotion dependencies");
    }

    private static async Task InstallAsync(InstallerOptions options)
    {
        if (!OperatingSystem.IsWindows() || !Environment.Is64BitOperatingSystem)
        {
            throw new PlatformNotSupportedException("This installer supports 64-bit Windows only.");
        }

        string installRoot = Path.GetFullPath(options.InstallRoot);
        string toolsRoot = Path.Combine(installRoot, "tools");
        string cacheRoot = Path.Combine(installRoot, "downloads");
        string pythonRoot = Path.Combine(installRoot, "python");
        string appRoot = Path.Combine(installRoot, "workflow", Version);
        string stagingRoot = Path.Combine(installRoot, ".staging", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(toolsRoot);
        Directory.CreateDirectory(cacheRoot);
        Directory.CreateDirectory(Path.GetDirectoryName(appRoot)!);
        Directory.CreateDirectory(stagingRoot);

        using HttpClient client = CreateHttpClient();
        try
        {
            Console.WriteLine("Downloading local tools...");
            string uvArchive = await DownloadVerifiedAsync(client, Uv, cacheRoot);
            string nodeArchive = await DownloadVerifiedAsync(client, Node, cacheRoot);
            string ffmpegArchive = await DownloadVerifiedAsync(client, Ffmpeg, cacheRoot);

            string uvPath = Path.Combine(toolsRoot, "uv.exe");
            ExtractSingleExecutable(uvArchive, "uv.exe", uvPath, stagingRoot);

            string nodeRoot = Path.Combine(toolsRoot, "node");
            ExtractDirectoryWithFile(nodeArchive, "node.exe", nodeRoot, stagingRoot);
            string ffmpegRoot = Path.Combine(toolsRoot, "ffmpeg");
            ExtractDirectoryWithFile(ffmpegArchive, "ffmpeg.exe", ffmpegRoot, stagingRoot);

            Console.WriteLine("Downloading the application source...");
            string sourceArchive = Path.Combine(stagingRoot, "videoedit-source.zip");
            await DownloadToFileAsync(
                client,
                $"{Repository}/archive/refs/tags/{ReleaseTag}.zip",
                sourceArchive,
                "application source"
            );
            if (Directory.Exists(appRoot) && File.Exists(Path.Combine(appRoot, "pyproject.toml")))
            {
                Console.WriteLine($"Keeping existing application at {appRoot}");
            }
            else
            {
                ExtractDirectoryWithFile(sourceArchive, "pyproject.toml", appRoot, stagingRoot);
            }

            string nodeExe = Path.Combine(nodeRoot, "node.exe");
            string npmCmd = Path.Combine(nodeRoot, "npm.cmd");
            string ffmpegExe = Path.Combine(ffmpegRoot, "bin", "ffmpeg.exe");
            string ffprobeExe = Path.Combine(ffmpegRoot, "bin", "ffprobe.exe");
            RequireFile(uvPath, "uv");
            RequireFile(nodeExe, "Node.js");
            RequireFile(npmCmd, "npm");
            RequireFile(ffmpegExe, "FFmpeg");
            RequireFile(ffprobeExe, "ffprobe");

            Dictionary<string, string> environment = BuildEnvironment(
                installRoot,
                appRoot,
                pythonRoot,
                uvPath,
                nodeExe,
                npmCmd,
                ffmpegExe,
                ffprobeExe,
                options.SkipWhisper ? null : Path.Combine(installRoot, "models", "small.pt")
            );

            Console.WriteLine("Installing managed Python 3.11...");
            Run(
                uvPath,
                new[] { "python", "install", "3.11", "--install-dir", pythonRoot, "--no-registry" },
                environment,
                installRoot
            );

            Console.WriteLine("Installing Python dependencies...");
            List<string> uvSyncArgs = new() { "sync", "--python", "3.11", "--extra", "dev" };
            if (!options.SkipWhisper)
            {
                uvSyncArgs.AddRange(new[] { "--extra", "whisper" });
            }
            Run(uvPath, uvSyncArgs, environment, appRoot);

            Console.WriteLine("Installing Remotion dependencies...");
            Run(npmCmd, "ci", environment, Path.Combine(appRoot, "remotion"));

            string modelPath = Path.Combine(installRoot, "models", "small.pt");
            if (!options.SkipWhisper)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(modelPath)!);
                await DownloadVerifiedToPathAsync(client, Whisper with { Name = WhisperModel }, modelPath);
            }

            WriteEnvironmentFile(appRoot, environment);
            WriteLauncher(installRoot, appRoot, uvPath, environment);
        }
        finally
        {
            TryDeleteDirectory(stagingRoot);
        }
    }

    private static HttpClient CreateHttpClient()
    {
        HttpClientHandler handler = new() { AutomaticDecompression = DecompressionMethods.All };
        HttpClient client = new(handler);
        client.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("VideoEditInstaller", Version));
        client.Timeout = TimeSpan.FromHours(2);
        return client;
    }

    private static async Task<string> DownloadVerifiedAsync(HttpClient client, DownloadSpec spec, string directory)
    {
        Directory.CreateDirectory(directory);
        string destination = Path.Combine(directory, SanitizeFileName(spec.Name) + ".download");
        if (File.Exists(destination) && await HashFileAsync(destination) == spec.Sha256)
        {
            Console.WriteLine($"  Reusing {spec.Name}");
            return destination;
        }

        await DownloadToFileAsync(client, spec.Url, destination, spec.Name);
        string actual = await HashFileAsync(destination);
        if (!actual.Equals(spec.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            TryDeleteFile(destination);
            throw new InvalidDataException($"{spec.Name} hash mismatch. Expected {spec.Sha256}, got {actual}.");
        }
        return destination;
    }

    private static async Task DownloadVerifiedToPathAsync(HttpClient client, DownloadSpec spec, string destination)
    {
        if (File.Exists(destination) && await HashFileAsync(destination) == spec.Sha256)
        {
            Console.WriteLine($"  Reusing {spec.Name}");
            return;
        }

        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        await DownloadToFileAsync(client, spec.Url, destination, spec.Name);
        string actual = await HashFileAsync(destination);
        if (!actual.Equals(spec.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            TryDeleteFile(destination);
            throw new InvalidDataException($"{spec.Name} hash mismatch. Expected {spec.Sha256}, got {actual}.");
        }
    }

    private static async Task DownloadToFileAsync(HttpClient client, string url, string destination, string label)
    {
        string staged = destination + ".part";
        TryDeleteFile(staged);
        using HttpResponseMessage response = await client.GetAsync(url, HttpCompletionOption.ResponseHeadersRead);
        response.EnsureSuccessStatusCode();
        await using Stream input = await response.Content.ReadAsStreamAsync();
        await using FileStream output = new(staged, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        byte[] buffer = new byte[1024 * 1024];
        long total = response.Content.Headers.ContentLength ?? 0;
        long received = 0;
        int read;
        while ((read = await input.ReadAsync(buffer)) > 0)
        {
            await output.WriteAsync(buffer.AsMemory(0, read));
            received += read;
            if (total > 0)
            {
                Console.Write($"\r  {label}: {received * 100 / total,3}%");
            }
        }
        await output.FlushAsync();
        Console.WriteLine();
        File.Move(staged, destination, true);
    }

    private static void ExtractSingleExecutable(string archive, string fileName, string destination, string stagingRoot)
    {
        string extractRoot = Path.Combine(stagingRoot, Path.GetFileNameWithoutExtension(fileName));
        ExtractArchive(archive, extractRoot);
        string source = Directory.GetFiles(extractRoot, fileName, SearchOption.AllDirectories).Single();
        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        File.Copy(source, destination, true);
    }

    private static void ExtractDirectoryWithFile(string archive, string fileName, string destination, string stagingRoot)
    {
        string extractRoot = Path.Combine(stagingRoot, Path.GetFileNameWithoutExtension(archive));
        ExtractArchive(archive, extractRoot);
        string file = Directory.GetFiles(extractRoot, fileName, SearchOption.AllDirectories).Single();
        string sourceRoot = Directory.GetParent(file)!.FullName;
        while (Directory.GetParent(sourceRoot) is not null &&
               Directory.GetParent(sourceRoot)!.FullName != extractRoot)
        {
            sourceRoot = Directory.GetParent(sourceRoot)!.FullName;
        }
        CopyDirectory(sourceRoot, destination);
    }

    private static void ExtractArchive(string archive, string destination)
    {
        TryDeleteDirectory(destination);
        Directory.CreateDirectory(destination);
        ZipFile.ExtractToDirectory(archive, destination, true);
    }

    private static Dictionary<string, string> BuildEnvironment(
        string installRoot,
        string appRoot,
        string pythonRoot,
        string uvPath,
        string nodeExe,
        string npmCmd,
        string ffmpegExe,
        string ffprobeExe,
        string? whisperPath)
    {
        string nodeRoot = Path.GetDirectoryName(nodeExe)!;
        string ffmpegRoot = Path.GetDirectoryName(ffmpegExe)!;
        string existingPath = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        Dictionary<string, string> values = new(StringComparer.OrdinalIgnoreCase)
        {
            ["VIDEOEDIT_WORKSPACE"] = appRoot,
            ["VIDEOEDIT_UV_PATH"] = uvPath,
            ["VIDEOEDIT_NODE_PATH"] = nodeExe,
            ["VIDEOEDIT_NPM_PATH"] = npmCmd,
            ["VIDEOEDIT_FFMPEG_PATH"] = ffmpegExe,
            ["VIDEOEDIT_FFPROBE_PATH"] = ffprobeExe,
            ["UV_PYTHON_INSTALL_DIR"] = pythonRoot,
            ["PATH"] = string.Join(Path.PathSeparator, nodeRoot, ffmpegRoot, Path.GetDirectoryName(uvPath)!, existingPath),
        };
        if (whisperPath is not null)
        {
            values["VIDEOEDIT_WHISPER_MODEL_PATH"] = whisperPath;
        }
        return values;
    }

    private static void WriteEnvironmentFile(string appRoot, IReadOnlyDictionary<string, string> values)
    {
        StringBuilder builder = new();
        foreach ((string key, string value) in values)
        {
            if (key is "PATH" or "UV_PYTHON_INSTALL_DIR" or "VIDEOEDIT_UV_PATH")
            {
                continue;
            }
            builder.Append(key).Append('=').Append(value.Replace("\\", "/")).AppendLine();
        }
        File.WriteAllText(Path.Combine(appRoot, ".env"), builder.ToString(), new UTF8Encoding(false));
    }

    private static void WriteLauncher(
        string installRoot,
        string appRoot,
        string uvPath,
        IReadOnlyDictionary<string, string> environment)
    {
        string launcher = Path.Combine(installRoot, "VideoEdit.cmd");
        string lines = $"@echo off{Environment.NewLine}" +
                       $"set \"VIDEOEDIT_WORKSPACE={appRoot}\"{Environment.NewLine}" +
                       $"set \"VIDEOEDIT_NODE_PATH={environment["VIDEOEDIT_NODE_PATH"]}\"{Environment.NewLine}" +
                       $"set \"VIDEOEDIT_NPM_PATH={environment["VIDEOEDIT_NPM_PATH"]}\"{Environment.NewLine}" +
                       $"set \"VIDEOEDIT_FFMPEG_PATH={environment["VIDEOEDIT_FFMPEG_PATH"]}\"{Environment.NewLine}" +
                       $"set \"VIDEOEDIT_FFPROBE_PATH={environment["VIDEOEDIT_FFPROBE_PATH"]}\"{Environment.NewLine}" +
                       $"set \"PATH={environment["PATH"]}\"{Environment.NewLine}" +
                       $"pushd \"{appRoot}\"{Environment.NewLine}" +
                       $"\"{uvPath}\" run videoedit %*{Environment.NewLine}" +
                       "set \"exit_code=%ERRORLEVEL%\"" + Environment.NewLine +
                       "popd" + Environment.NewLine +
                       "exit /b %exit_code%" + Environment.NewLine;
        File.WriteAllText(launcher, lines, Encoding.ASCII);
    }

    private static void Run(
        string executable,
        string arg1,
        string arg2,
        string arg3,
        string arg4,
        Dictionary<string, string> environment,
        string workingDirectory)
    {
        Run(executable, new[] { arg1, arg2, arg3, arg4 }, environment, workingDirectory);
    }

    private static void Run(string executable, string argument, Dictionary<string, string> environment, string workingDirectory)
    {
        Run(executable, new[] { argument }, environment, workingDirectory);
    }

    private static void Run(string executable, IEnumerable<string> arguments, Dictionary<string, string> environment, string workingDirectory)
    {
        ProcessStartInfo info = new()
        {
            FileName = executable,
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = false,
        };
        foreach (string argument in arguments)
        {
            info.ArgumentList.Add(argument);
        }
        foreach ((string key, string value) in environment)
        {
            info.Environment[key] = value;
        }
        using Process process = Process.Start(info) ?? throw new InvalidOperationException($"Could not start {executable}.");
        process.OutputDataReceived += (_, e) => { if (e.Data is not null) Console.WriteLine(e.Data); };
        process.ErrorDataReceived += (_, e) => { if (e.Data is not null) Console.Error.WriteLine(e.Data); };
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        process.WaitForExit();
        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException($"{Path.GetFileName(executable)} exited with code {process.ExitCode}.");
        }
    }

    private static async Task<string> HashFileAsync(string path)
    {
        await using FileStream stream = File.OpenRead(path);
        byte[] hash = await SHA256.HashDataAsync(stream);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static void CopyDirectory(string source, string destination)
    {
        Directory.CreateDirectory(destination);
        foreach (string directory in Directory.GetDirectories(source, "*", SearchOption.AllDirectories))
        {
            Directory.CreateDirectory(Path.Combine(destination, Path.GetRelativePath(source, directory)));
        }
        foreach (string file in Directory.GetFiles(source, "*", SearchOption.AllDirectories))
        {
            string target = Path.Combine(destination, Path.GetRelativePath(source, file));
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.Copy(file, target, true);
        }
    }

    private static void RequireFile(string path, string label)
    {
        if (!File.Exists(path))
        {
            throw new FileNotFoundException($"{label} was not found after extraction.", path);
        }
    }

    private static string SanitizeFileName(string value) => string.Concat(value.Select(character =>
        Path.GetInvalidFileNameChars().Contains(character) ? '_' : character));

    private static void TryDeleteFile(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
    }

    private static void TryDeleteDirectory(string path)
    {
        try { if (Directory.Exists(path)) Directory.Delete(path, true); } catch { }
    }
}
