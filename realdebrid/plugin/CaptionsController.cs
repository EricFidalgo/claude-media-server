using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.RealDebrid;

/// <summary>The Captions page, and the only way in to the captions service from outside Docker, for Jellyfin admins only.</summary>
[ApiController]
[ApiExplorerSettings(IgnoreApi = true)]
[Route("Captions")]
public class CaptionsController : ControllerBase
{
    internal static readonly Uri Service = new(Environment.GetEnvironmentVariable("CAPTIONS_SERVICE") ?? "http://captions:8098/");
    private readonly IHttpClientFactory _clients;

    public CaptionsController(IHttpClientFactory clients)
    {
        _clients = clients;
    }

    /// <summary>The page itself. It holds nothing private, and everything it shows comes through Forward below.</summary>
    [HttpGet("app")]
    [AllowAnonymous]
    public IActionResult App()
    {
        var page = typeof(Plugin).Assembly.GetManifestResourceStream("RealDebrid.captions.html")!;
        Response.Headers.CacheControl = "no-cache";
        Response.Headers.ContentSecurityPolicy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'";
        return File(page, "text/html; charset=utf-8");
    }

    [AcceptVerbs("GET", "POST")]
    [Route("api/{**path}")]
    [Authorize(Policy = "RequiresElevation")]
    public Task<IActionResult> Forward(string path, CancellationToken cancellationToken)
        => ServiceProxy.Forward(Request, _clients, Service, "captions", path, cancellationToken);
}
