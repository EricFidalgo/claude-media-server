using System;
using System.IO;
using System.Net.Http;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.RealDebrid;

/// <summary>Passes a request from one of the pages to its service on the Docker network, and the answer back.</summary>
internal static partial class ServiceProxy
{
    private const long MaxBody = 6 * 1024 * 1024;

    public static async Task<IActionResult> Forward(HttpRequest incoming, IHttpClientFactory clients, Uri service, string serviceName, string path, CancellationToken cancellationToken)
    {
        if (!SafePath().IsMatch(path))
        {
            return Answer(400, "{\"error\":\"Unknown item.\"}");
        }

        using var request = new HttpRequestMessage(new HttpMethod(incoming.Method), new Uri(service, path));
        if (HttpMethods.IsPost(incoming.Method))
        {
            if (incoming.ContentLength > MaxBody)
            {
                return Answer(413, "{\"error\":\"That's too big.\"}");
            }

            using var body = new MemoryStream();
            var buffer = new byte[81920];
            int read;
            while ((read = await incoming.Body.ReadAsync(buffer, cancellationToken).ConfigureAwait(false)) > 0)
            {
                body.Write(buffer, 0, read);
                if (body.Length > MaxBody)
                {
                    return Answer(413, "{\"error\":\"That's too big.\"}");
                }
            }

            request.Content = new ByteArrayContent(body.ToArray());
        }

        var client = clients.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(60);
        try
        {
            using var response = await client.SendAsync(request, cancellationToken).ConfigureAwait(false);
            return Answer((int)response.StatusCode, await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false));
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException && !cancellationToken.IsCancellationRequested)
        {
            return Answer(502, "{\"error\":\"The " + serviceName + " service isn't running.\"}");
        }
    }

    private static ContentResult Answer(int status, string json) => new() { StatusCode = status, ContentType = "application/json", Content = json };

    [GeneratedRegex("^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")]
    private static partial Regex SafePath();
}
