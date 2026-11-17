# Signal Identifier

Listens to your radio through your computer's sound card and tells you what kind
of signal you're hearing: **CW, SSB voice, FT8, FT4, RTTY, PSK31**, a steady
carrier, or just noise. It shows a live waterfall with each signal labelled.

![modes](https://img.shields.io/badge/modes-CW%20%7C%20SSB%20%7C%20FT8%20%7C%20FT4%20%7C%20RTTY%20%7C%20PSK31-blue)

## Getting it running (Windows)

1. **Install Python (one time only).** Go to <https://www.python.org/downloads/>,
   click the yellow **Download** button and run the installer.
   **Tick "Add python.exe to PATH"** at the bottom of the first screen, then
   click **Install Now**.
2. **Download this program.** On this GitHub page, click the green **Code**
   button, then **Download ZIP**.
3. **Unzip it.** Open your Downloads folder, right-click the ZIP file, choose
   **Extract All…**, then click **Extract**.
4. **Start it.** In the extracted folder, double-click
   **`Start Signal Identifier.bat`**.
   - If Windows shows a blue "Windows protected your PC" box, click
     **More info**, then **Run anyway**.
   - The first start takes a minute or two while it gets set up. After that
     it opens quickly.
   - Keep the black window open while you use the program.

## Using it

1. Press **Demo** first to see what it looks like with made-up signals.
2. Pick your radio's sound input from the **Sound input** list. For radios with
   a built-in USB sound card (IC-7300, FT-991A, etc.) it's usually called
   **"USB Audio CODEC"**. If one entry doesn't work, try the same name with
   **[MME]** after it.
3. Press **Start listening**. Tune your radio to USB (upper sideband) for
   digital modes, as you would for WSJT-X or fldigi.
4. Watch the **Audio level** bar. It should say **OK**. If it says "Too loud"
   or "Very quiet", adjust the audio level on the radio or in Windows sound
   settings.

Results update every second, based on the last 15 seconds of audio. FT8 is
easiest to recognise after a full 15-second cycle.

## Good to know

- This **identifies** the mode; it doesn't decode the text. To read the
  messages, use WSJT-X (FT8/FT4) or fldigi (RTTY, PSK31, CW).
- It's a best guess, shown as "very likely", "likely" or "maybe". Very weak
  signals, heavy noise, or several signals on top of each other can confuse it.
- The tests so far use computer-generated signals, not recordings of real
  radio. Real-world results may need some tuning. Please report anything it
  gets wrong.

## For tinkerers

- Analyse a WAV recording: `python -m signal_identifier recording.wav`
- Run the tests: `pip install pytest` and then `python -m pytest`
- How it works: `signal_identifier/classify.py` measures each signal's width,
  keying, tone hopping and loudness pattern and matches them against what each
  mode looks like. No internet connection or AI service is used.
