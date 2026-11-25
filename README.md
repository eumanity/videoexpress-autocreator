# VideoExpress AutoCreator

The app offers visual presets for cinematic photorealism, 3D, 2D, watercolor, Stickman, and Claymation/stop-motion. A video can be up to 18 minutes. Individual VideoExpress clips are limited to 10 seconds; scene settings are capped accordingly, and older overlong scenes are split into shorter clips while preserving unaffected rendered clips. VideoExpress exports are limited to 10 minutes each, so longer videos are exported as consecutive segments and joined locally on the user's PC with ffmpeg. Segment URLs and downloaded files are saved in project state so an interrupted export can resume.

To join segments, the app uses the ffmpeg binary already used for audio mixing (`imageio-ffmpeg`, or `ffmpeg` on `PATH`). VideoExpress still needs to be available for the browser-driven render and export steps.

Install the Python dependencies with `python -m pip install -r requirements.txt`. Install Playwright's fallback browser once with `python -m playwright install chromium`; the app will prefer the configured Chrome/Edge browser and use this Chromium if it cannot launch that browser.

## Windows executable

Download the Windows x64 executable from [GitHub Releases](https://github.com/eumanity/videoexpress-autocreator/releases). Google Chrome or Microsoft Edge must be installed for VideoExpress browser automation. The app also requires a VideoExpress account and an AI provider key for AI-generated scripts and prompts. On first launch, Windows may show a SmartScreen warning because the executable is not code-signed.

The executable and source are provided under the proprietary terms in [LICENSE](LICENSE): users may download and run the unmodified executable, but may not modify or redistribute it.

To build the executable from source on Windows, run `.\build_windows.ps1`. The executable keeps settings, API keys, browser data, and projects in the user's Documents folder; these are not included in the release.
