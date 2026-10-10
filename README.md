# Thai Club Mixer 🇹🇭

A QR-code web game for club mixers. Guests scan, type their name, and play from their phone:

- **Bingo**: "find someone who…", a 5×5 card with points per square and a bonus for each full line
- **Team up**: challenges where a น้อง teams up with a พี่ (upperclassman), proved with a photo or video
- **Perform**: show something Thai, sing a Thai song… on video
- **Leaderboard**: live points and rank

Photos and videos go to an **admin queue**. Approved ones appear on the **projector page** next to the leaderboard and a QR code. Points count as soon as something is submitted; rejecting a submission takes the points away, and the player can resubmit.

| Page | Who | What |
|---|---|---|
| `/` | guests | join, then play |
| `/screen` | projector | live photo/video feed, top 10, QR |
| `/admin` | organizers | approve or reject media, edit activities, fix anyone's score |
| `/qr` | print it | a big QR code pointing at the site |

## Change the activities

Log into `/admin` and open **Edit activities**. From there you can rewrite any bingo square, add, reorder or delete challenges and performances, and change points and what proof each one needs. Edits are saved in the database, and players' phones pick them up within about 15 seconds.

`content.py` holds the starting activities, which are used until something is saved in the editor. **Reset** in the editor goes back to them.

## Fix scores

Tap a player's name on the admin leaderboard to see everything they've submitted, including bingo names, which never go through the approval queue. **Remove points** takes off the points for anything that doesn't count, and the player can redo it. You can also delete a player entirely, for example someone who joined with a joke name. Each row of the leaderboard has a **Delete** button for this.

To save every photo and video, click **⬇️ Download photos** in the admin menu. You get one zip with readable names like `012 Ploy - Team up with a พี่ for a wai selfie.jpg` (rejected ones are left out). Locally they also sit in the `uploads/` folder, with random names.

Photos and videos on the projector can be given a caption, **hidden** (off the screen, but the person keeps their points) or **deleted** (the points come off too).

After a practice run, use **Reset** at the bottom of `/admin`, where you type RESET to confirm. You can clear all scores and photos but keep the players, or delete everyone. Edited activities are kept either way.

## Run it on your computer

```bash
pip install -r requirements.txt
python app.py
```

To use `/admin`, first create a `.env` file containing `ADMIN_PASSWORD=pick-something`. Then open http://localhost:5050. With no other settings it uses a local SQLite file (`mixer.db`) and an `uploads/` folder. To try it on your phone, use your computer's local IP (printed at startup) on the same Wi-Fi.

## Host it for the event

Most hosts wipe the server's disk on redeploy, so for the real event, store data and files in **Supabase** (free tier):

1. Create a Supabase project.
2. Set these environment variables on your host (see `.env.example`):
   - `DATABASE_URL`: Project Settings → Database → Connection string → **Session pooler**
   - `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (the **service_role** key, which is secret and only goes on the server): Project Settings → API
   - `ADMIN_PASSWORD`: admin stays off until this is set
   - `FLASK_SECRET_KEY`: any long random string (`python -c "import secrets; print(secrets.token_hex(32))"`)
3. Deploy to any Python host. On first start the app creates its tables (with row-level security on) and a public `mixer-media` storage bucket by itself.
   - **Render (easiest)**: Render dashboard → **New → Blueprint** → pick this repo. `render.yaml` sets everything up, and Render asks you for the Supabase values and `ADMIN_PASSWORD`.
   - **Vercel and other "serverless" hosts don't work as is**: they can't save files and cap uploads at about 4.5 MB.
   - **Railway / Heroku-style hosts**: the `Procfile` is already set up.
4. Open `/qr` on the live site and print it, open `/screen` on the projector laptop, and log into `/admin` on an organizer's phone.

Free hosts (such as Render's free tier) go to sleep when no one is using them, so open the site a few minutes before the event starts.

## Good to know

- **Limits:** videos can be 30 s at most and files 50 MB at most (Supabase's free per-file cap). Photos are shrunk on the phone before upload, so they're fast even on bad Wi-Fi.
- **iPhone videos** are often HEVC `.mov`, which Chrome on Windows may not play. Run the projector page in **Safari, or Edge with the HEVC extension**, or test one clip beforehand.
- Joining doesn't need a password. If someone clears their browser they can't get back into their old player, so they join again under a slightly different name.
- Media links are random and not listed anywhere, but anyone who has a link can open it. Rejecting a submission deletes its file.
