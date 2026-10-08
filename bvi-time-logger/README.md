# BVI Time Logger

Logs which window you have open every few seconds, so you can fill in your Deltex timesheet from it.
Each person runs it on their own PC. Nothing gets sent anywhere. Logs are saved only in the folder you pick.

## First time

1. Double-click `BVI_Time_Logger.exe`.
2. A window pops up:
   - **Save logs in:** browse to your folder (e.g. your User drive).
   - **Log every:** 15 seconds is the default.
   - **Idle after:** with no mouse or keyboard for this long, time is logged as Idle.
   - **Start automatically:** leave it ticked so you never miss a day.
3. Click Save. A clock icon appears next to the Windows clock. That's it.

Settings are remembered in `%APPDATA%\BVI Time Logger\settings.json`.

## Tray icon (right-click)

- **Open log folder**
- **Pause / Resume** for private stuff you don't want logged
- **Settings...** to change folder or interval
- **Exit**

## What gets logged

One file per day: `TimeLog_<PC>_<date>.csv`

| Column | Meaning |
|---|---|
| Timestamp, Date | When the sample was taken |
| Seconds | Time this row counts for (the interval) |
| Status | Active, Idle or Locked |
| Project | BVI project number found in the window title (e.g. `35515.00`). Blank if none found, so the Excel puts it on 90011.00 by default |
| App, Window Title | What was open |
| PC Name, User | Whose PC |

Tip: start file names with the project number, e.g. `33236.19 Layout rev2.xlsx`, so time is picked up automatically.

If the CSV is open in Excel, the logger can't write to it. It keeps the rows and saves them as soon as the file is closed.
If something goes wrong, check `%APPDATA%\BVI Time Logger\errors.txt`.

## Getting the exe

GitHub builds it automatically: Actions tab, "Build BVI Time Logger", latest run, download `BVI_Time_Logger` under Artifacts.

To run from source instead: `pip install -r requirements.txt` then `pythonw time_logger.py`.
