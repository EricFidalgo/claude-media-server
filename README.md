# Movies (Jellyfin)

Phone and PC: `https://ubuntu.<your-tailnet>.ts.net:8096` (works only on the tailnet). Your tailnet name goes in `local.env`, which git skips: copy `local.env.example` to start. On iPhone, use the Jellyfin app you already have with that address. Swiftfin is a free alternative.

Put movies in `/mnt/hdd/Movies/Title (Year)/Title (Year).mkv` and shows in `/mnt/hdd/Shows/Show/Season 01/Show S01E01.mkv`. Jellyfin reads those folders and can't change them.

**Home Movies** is `/mnt/hdd/Home Movies` (your own videos). It's a "Home videos and photos" library with internet lookups off, so file names are the titles. To add more, drop files into a folder there. The nightly backup doesn't copy it, and the backups live on the same HDD anyway, so keep the originals somewhere else.

## Real-Debrid app

Admins open **Real-Debrid** from Jellyfin's side menu or user menu. It's its own simple page, outside Jellyfin's screens: search what's already on the Real-Debrid account, tap **Download**, and the video lands on this server and appears in Jellyfin by itself. A pack asks which videos first. Search looks at torrent names only, so a film inside a collection is found by the collection's name. The Jellyfin iPhone app should show the menu entry too, but that hasn't been tried. Swiftfin and Apple TV don't show it.

Names follow the AIOStreams format: `Movie Title (2024) BluRay [🇺🇸]`, then `2160p [6GB]` below it. A name that doesn't mention a language counts as English, unless its title isn't in the Latin alphabet: then it gets no flag.

The folder comes from the names too. A torrent's only video goes by the torrent's name, unless the file's own name has a year or an episode. `Ep1` or `05` in a single-season pack becomes that episode. Samples, extras and a disc's short streams start unticked.

- `realdebrid/plugin` runs inside Jellyfin. It serves the page, passes admin requests to the service, adds the menu entries, and scans the library whenever a download finishes. It has to scan because Jellyfin doesn't watch a library created on an empty folder until it has something in it and Jellyfin restarts.
- `realdebrid/service` is the `realdebrid` container. It holds the token from `~/.claude-secrets/env/REALDEBRID_API_TOKEN`, writes into the library, and keeps its queue in `realdebrid/state`. Files on their way in wait in a hidden `.saving` folder in each library. Jellyfin stays read-only.
- **Kept but not on the page:** the service can still add magnets and torrent files, and cover links, hosts, points and traffic. The page has no buttons for them.
- **Changing the key:** open **Real-Debrid key** at the bottom of the page and paste a key from real-debrid.com/apitoken. Real-Debrid checks it first, so a typo never replaces a working key. It's saved in `realdebrid/state/token`, so the nightly backup copies it, and it wins over the key file in the secrets folder, which stays as the fallback. The box also opens by itself when there's no key or Real-Debrid turns it down. Only Real-Debrid is supported. Other debrid services use different APIs, so each would need its own code.
- **Who can reach it:** the page itself loads without a login and holds nothing private. Everything it shows comes through the pass-through, which checks for a Jellyfin admin. The container has no login of its own and publishes no port, so tailnet devices and guests can't reach it, but other programs on this machine and containers on its network can.
- **After changing the page or plugin:** run `realdebrid/plugin/build.sh`, then `docker compose restart jellyfin`.
- **After changing the service:** `docker compose restart realdebrid`.
- **Tests:** `cd realdebrid/service && python3 -m unittest` for the engine, and `realdebrid/e2e/run.sh` for a full run on a phone-sized screen. That uses a throwaway Jellyfin and a fake Real-Debrid, so it can't touch the real server, library or account.

## Captions

Admins open **Captions** from Jellyfin's side menu, or go to `https://ubuntu.<your-tailnet>.ts.net:8096/Captions/app`. Find a video, tick the captions to remove, and tap the button twice. To add one, pick the language and choose a `.srt`, `.vtt`, `.ass` or `.ssa` file. Jellyfin picks up each change by itself within about a minute.

- **Removing is permanent.** To get a caption back, upload a caption file or download the video again. There's no undo.
- **Built-in captions:** taking one out rewrites the video without re-encoding it, so picture and sound don't change. The new copy waits in a hidden `.captions` folder in the library and only replaces the original once its tracks and length check out. A failure leaves the original alone. It takes seconds to a few minutes, and it's safe to leave the page.
- **Added captions** are plain files beside the video: `Title (Year).en.srt`.
- **Limits:** one video at a time, not a whole season. Captions burned into the picture can't be removed. Removing a built-in caption while someone watches that video may stop their playback.
- `captions` is its own container with ffmpeg, separate from the Real-Debrid token. It's the only other service that can write to the library.
- **Who can reach it:** the same as Real-Debrid. The page holds nothing private, everything goes through the admin-only pass-through, and the container publishes no port. Anyone who isn't an admin sees no menu entry and gets "Captions is for admins only".
- **After changing the page or plugin:** run `realdebrid/plugin/build.sh`, then `docker compose restart jellyfin`.
- **After changing the service:** `docker compose restart captions`.
- **Tests:** `cd captions && python3 -m unittest`, and `realdebrid/e2e/run.sh` also covers this page.

## How it behaves

- **At home:** full quality. Wi-Fi isn't the limit.
- **Away from home:** everything goes through the home upload, about 5 to 6 Mbit/s here. That's one good stream. If your own phone buffers, set the player's quality to about 4 Mbit/s, because your devices aren't capped.
- **Guests:** capped at 4 Mbit/s automatically. Two at once will buffer. 4K over the internet won't work.
- **Disk:** the HDD has room for about 400 movies at 4 GB each, or 120 to 170 at 10 to 15 GB.
- **GPU:** an Nvidia RTX 2070 does the transcoding at about 5 to 6 times real speed for 1080p. HDR tone mapping is on but hasn't been tried on a real HDR file.

## First run (once)

Open the link above and follow the wizard:

1. Create your admin user with a strong password.
2. Add a library: content type **Movies**, folder `/media/movies`, and turn **Real-time monitoring** on.
3. Add a second library: content type **Shows**, folder `/media/shows`, and turn **Real-time monitoring** on.
4. Leave **Allow remote connections** on. The tailnet counts as remote.

## Share with a guest

**Easiest:** `bash ~/media-stack/share-with-guest.sh` walks through every step below, including the guest's TV. Xumo TVs can't install Jellyfin or Tailscale, and Roku can't run Tailscale, so the guest needs a Fire TV Stick (not the 4K Select) or a Google TV Streamer.

A Jellyfin user without admin rights can only watch: it can't add, delete or rename anything, and the Real-Debrid and Captions pages refuse it. Limit what it sees by unticking "access to all libraries" on that user.

Never share the `ubuntu` machine: that would open the Money app and the Docker panel too. Share `movies`, a separate tailnet device that reaches only Jellyfin. Tested so far: the container starts, prints its login link and reaches Jellyfin. It hasn't joined the tailnet or served a guest yet, so check step 3 with the first guest.

1. Start it: `cd ~/media-stack && docker compose --profile share up -d movies-share`
2. Run `docker logs movies-share` and open the login link it prints. Then, in the Tailscale admin console, turn off key expiry for `movies`.
3. Machines → `movies` → Share → send the invite. The guest needs a free Tailscale account and opens `https://movies.<your-tailnet>.ts.net`.
4. In Jellyfin: Dashboard → Users → add a user for them, and leave admin rights off.

Remove a guest by revoking the share in the Tailscale admin console and deleting their Jellyfin user. Stop sharing altogether with `docker compose --profile share stop movies-share`.

Sharing commercial movies outside your household is a copyright issue, and that's your call to make.

## Maintenance

- **New device of your own on the tailnet:** it counts as a guest until you run the following, which gives it full quality:
  ```bash
  cd ~/media-stack && docker compose stop jellyfin && python3 configure_jellyfin.py jellyfin/config 172.30.96 && docker compose start jellyfin
  ```
- **Transcoding stops after an Nvidia driver update:** `docker-compose.yml` mounts the driver's libraries directly, because the proper tool needs sudo. Installing `nvidia-container-toolkit` is the lasting fix.
- **Backups:** The nightly backup keeps the Jellyfin database and settings and the Real-Debrid app's queue. It doesn't copy the movies or shows.
