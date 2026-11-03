using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Library;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.RealDebrid;

/// <summary>Scans the library when the realdebrid service finishes saving something or the captions service changes something. Jellyfin's own watching can't be relied on: a library created on an empty folder isn't watched until it has something in it and Jellyfin restarts.</summary>
public class LibraryScanner : BackgroundService
{
    private static readonly TimeSpan Every = TimeSpan.FromSeconds(10);
    private readonly IHttpClientFactory _clients;
    private readonly ILibraryManager _library;
    private readonly ILogger<LibraryScanner> _logger;

    public LibraryScanner(IHttpClientFactory clients, ILibraryManager library, ILogger<LibraryScanner> logger)
    {
        _clients = clients;
        _library = library;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // Starts empty, so a restart scans once too, and anything saved while Jellyfin was down still appears.
        var known = new HashSet<string>();
        using var timer = new PeriodicTimer(Every);
        while (await timer.WaitForNextTickAsync(stoppingToken).ConfigureAwait(false))
        {
            try
            {
                var finished = new HashSet<string>();
                await AddFinishedSaves(finished, stoppingToken).ConfigureAwait(false);
                await AddCaptionChange(finished, stoppingToken).ConfigureAwait(false);
                if (!finished.IsSubsetOf(known))
                {
                    _logger.LogInformation("Real-Debrid saved something new or captions changed, so scanning the library");
                    await _library.ValidateMediaLibrary(new Progress<double>(), stoppingToken).ConfigureAwait(false);
                    known.UnionWith(finished); // only after the scan started, so a failed one is tried again next time
                }
            }
            catch (Exception e) when (!stoppingToken.IsCancellationRequested)
            {
                _logger.LogWarning("Couldn't scan the library, trying again shortly: {Reason}", e.Message);
            }
        }
    }

    /// <summary>A Real-Debrid service that's down mustn't stop caption changes from appearing.</summary>
    private async Task AddFinishedSaves(HashSet<string> seen, CancellationToken cancellationToken)
    {
        var client = _clients.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(10);
        try
        {
            using var answer = await client.GetStreamAsync(new Uri(RealDebridController.Service, "queue"), cancellationToken).ConfigureAwait(false);
            using var jobs = await JsonDocument.ParseAsync(answer, cancellationToken: cancellationToken).ConfigureAwait(false);
            foreach (var job in jobs.RootElement.EnumerateArray())
            {
                if (job.GetProperty("status").GetString() == "done")
                {
                    seen.Add(job.GetProperty("id").GetString()!);
                }
            }
        }
        catch (Exception e) when (e is HttpRequestException or JsonException or TaskCanceledException && !cancellationToken.IsCancellationRequested)
        {
            _logger.LogDebug("Couldn't check the Real-Debrid queue: {Reason}", e.Message);
        }
    }

    /// <summary>A captions service that's down mustn't stop new downloads from appearing.</summary>
    private async Task AddCaptionChange(HashSet<string> seen, CancellationToken cancellationToken)
    {
        var client = _clients.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(10);
        try
        {
            using var answer = await client.GetStreamAsync(new Uri(CaptionsController.Service, "changes"), cancellationToken).ConfigureAwait(false);
            using var changes = await JsonDocument.ParseAsync(answer, cancellationToken: cancellationToken).ConfigureAwait(false);
            var latest = changes.RootElement.GetProperty("latest").GetString();
            if (latest is not null)
            {
                seen.Add("captions:" + latest);
            }
        }
        catch (Exception e) when (e is HttpRequestException or JsonException or TaskCanceledException && !cancellationToken.IsCancellationRequested)
        {
            _logger.LogDebug("Couldn't check the captions service: {Reason}", e.Message);
        }
    }
}
