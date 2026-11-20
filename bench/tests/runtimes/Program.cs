// Copy a file, send it over a socket in the three ways the library has, copy one connection into
// another and serve it over HTTP, each through the library call a C# program would use.
using System.Net;
using System.Net.Sockets;

var mode = args[0];
if (mode == "copy")
{
    File.Copy("in.bin", "out.bin", true);
    Console.WriteLine("ok");
    return;
}
if (mode == "serve")
{
    var builder = WebApplication.CreateBuilder();
    builder.Logging.ClearProviders();
    var app = builder.Build();
    app.MapGet("/in.bin", () => Results.File(Path.GetFullPath("in.bin"), "application/octet-stream"));
    app.MapGet("/quit", () =>
    {
        Task.Run(async () => { await Task.Delay(300); Environment.Exit(0); });
        return "bye";
    });
    app.Run("http://127.0.0.1:18080");
    return;
}

var listener = new TcpListener(IPAddress.Loopback, 0);
listener.Start();
var taking = Task.Run(() =>
{
    using var c = listener.AcceptSocket();
    using var stream = new NetworkStream(c);
    using var file = File.Create("got.bin");
    stream.CopyTo(file);
});
try
{
    using var s = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
    s.Connect(listener.LocalEndpoint);
    if (mode == "send")
    {
        s.SendFile("in.bin");
    }
    else if (mode == "send-async")
    {
        s.SendFileAsync("in.bin").AsTask().Wait();
    }
    else if (mode == "send-packets")
    {
        var done = new ManualResetEventSlim();
        var e = new SocketAsyncEventArgs { SendPacketsElements = new[] { new SendPacketsElement("in.bin") } };
        e.Completed += (_, _) => done.Set();
        if (s.SendPacketsAsync(e)) done.Wait();
        if (e.SocketError != SocketError.Success) throw new SocketException((int)e.SocketError);
    }
    else if (mode == "relay")
    {
        // what arrives on one connection is copied into the one that is read
        var feeder = new TcpListener(IPAddress.Loopback, 0);
        feeder.Start();
        var feeding = Task.Run(() =>
        {
            using var c = new TcpClient();
            c.Connect((IPEndPoint)feeder.LocalEndpoint);
            using var file = File.OpenRead("in.bin");
            file.CopyTo(c.GetStream());
        });
        using var from = feeder.AcceptTcpClient();
        using var to = new NetworkStream(s);
        from.GetStream().CopyTo(to);
        feeding.Wait();
    }
    s.Shutdown(SocketShutdown.Both);
}
catch (Exception error)
{
    var inner = error is AggregateException many ? many.InnerException! : error;
    Console.WriteLine("refused: " + inner.GetType().Name + ": " + inner.Message);
    return;
}
taking.Wait();
Console.WriteLine("ok");
