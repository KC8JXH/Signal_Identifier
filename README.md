# Signal Identifier

Listens to your radio through your computer's sound card and tells you what kind
of signal you're hearing: **CW, SSB voice, FT8, FT4, RTTY, PSK31**,
Winlink-style **ARQ data bursts** (ARDOP, VARA, PACTOR), a steady carrier, or
just noise. It shows a live waterfall with each signal labelled.

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

1. Pick your radio's sound input from the **Sound input** list. For radios with
   a built-in USB sound card (IC-7300, FT-991A, etc.) it's usually called
   **"USB Audio CODEC"**. If one entry doesn't work, try the same name with
   **[MME]** after it.
2. Press **Start listening**. Tune your radio to USB (upper sideband) for
   digital modes, as you would for WSJT-X or fldigi.
3. Watch the **Audio level** bar. It should say **OK**. If it says "Too loud"
   or "Very quiet", adjust the audio level on the radio or in Windows sound
   settings.

Results update every second, based on the last 15 seconds of audio. FT8 is
easiest to recognise after a full 15-second cycle.

## Showing the radio's frequency (IC-7300 and others)

The program can read the dial frequency, mode and S-meter from your radio.
With that you get:

- UTC time, frequency (laid out like the IC-7300's, **14.074.00**) and mode
  in the top right corner
- an **S-meter** bar next to the audio level
- the waterfall scale labelled in radio frequencies
- on Icom radios, a **whole-band view** from the radio's own band scope (see below)

It never transmits. The only things it ever changes on the radio are the
frequency and mode, and only when you click a signal (with "Click a signal to
tune the radio" ticked) or press a band or mode button.

**IC-7300, step by step**

1. Connect the radio to the computer with the USB cable and turn the radio on.
   (If Windows doesn't recognise it, install the **Icom USB driver** from
   Icom's website.)
2. In the **Radio** list choose **Icom IC-7300**.
3. In **Port**, pick the entry that says **Silicon Labs CP210x … (COMx)**
   (it's usually picked for you). Press **↻** if the list is empty.
4. Leave **Speed** at **115200**. On the radio, go to **MENU → SET →
   Connectors → CI-V** and set:
   - **CI-V USB Baud Rate**: **115200**
   - **CI-V USB Port**: **Unlink from [REMOTE]** (needed for the whole-band view)
5. Press **Connect radio**. The frequency and mode appear above the results.
   Next time it reconnects automatically.

Also supported: other Icoms (IC-705, IC-7610, IC-9700, IC-7100, …), Yaesu
(FT-991A, FT-891, FT-710, FTDX10/101, FT-817/818/857/897), Kenwood and
Elecraft. Set **Speed** to match the CAT rate set in your radio's menu.

### Whole-band view (Icom radios)

The strip above the detailed waterfall shows the whole band from the radio's
built-in spectrum scope, with coloured hints for where CW, FT8, FT4, RTTY and
SSB are usually found (US band plan). The dashed box is the slice you're
listening to, which is what the detailed waterfall shows and what the program
identifies.

- **Click** in the band view to bring that spot into the detailed waterfall:
  in USB/LSB/data modes it lands in the middle of your audio, in CW mode right
  on your pitch. (The band scope is coarse, about 700 Hz per point on 20 m,
  so this is the reliable way to get a signal into view.)
- **Click** in the detailed waterfall and the radio goes to exactly the
  frequency shown under the pointer.
- Turn off "Click a signal to tune the radio" if you don't want clicks to tune.
- **Show whole band** switches the radio's scope to centre mode, ±500 kHz,
  which covers any band up to 500 kHz wide. Or set the scope to Fixed mode
  with the band edges on the radio itself; the program shows whatever the
  scope shows.
- The scope only has 475 points across the screen (about 700 Hz each on
  20 m), so it shows *where* signals are. Identifying the mode still needs
  them to be in the detailed (audio) view.

**Using WSJT-X at the same time?** Only one program can use the radio's COM
port at once. To share it, run **flrig** (free), point both WSJT-X and this
program at it (choose **flrig** in the Radio list), or use **Hamlib rigctld**.

On-air frequencies are worked out for USB, LSB and data modes (USB-D, etc.)
and for CW. In CW mode the frequency shown is what the radio would display if
you tuned to that signal (the IC-7300's CW pitch is read automatically; other
radios assume 600 Hz). In AM or FM mode the audio frequency is shown instead.

The S-meter uses Icom's published scale for Icom radios and rigctld's
calibrated reading. For other radios it's approximate.

## Recording

Press **● Record** to start recording the receiver audio, and **■ Stop rec**
to finish. The button shows how long you've been recording. The file goes in
the `recordings` folder (the folder opens when you stop), named with the time
(UTC) and, if the radio is connected, its frequency and mode.

Recordings of signals the program gets wrong or can't identify are the best
way to improve it. To share one, open this project on GitHub, click
**Add file → Upload files**, drag the WAV file in and click **Commit changes**.

You can also analyse a saved file: `python -m signal_identifier recordings\yourfile.wav`

## Band and mode buttons

With the radio connected:

- **Band buttons** (160 … 6) above the band view change band. Each band
  remembers where you last were. The first time, it goes to the FT8 frequency
  if you're in a data mode (USB-D), the CW segment if you're in CW, or the
  phone segment if you're in SSB (LSB below 10 MHz, USB above). Pressing the
  band you're already on jumps back to that default spot.
- **Mode buttons** (LSB, USB, LSB-D, USB-D, CW, RTTY, AM, FM), next to the
  frequency display, change mode. The current band and mode are highlighted.

## About "ARQ data" labels

Winlink-type stations (ARDOP, VARA, PACTOR) send data in bursts lasting from a
fraction of a second to several seconds, taking turns with the other station.
The program labels these **ARQ data** with their width, and **ARDOP** when it
sees ARDOP's 4 tones 50 Hz apart. It can't yet tell VARA from PACTOR, and a
2,300 Hz-wide signal that fills the whole filter is still hard for it.

## Good to know

- This **identifies** the mode; it doesn't decode the text. To read the
  messages, use WSJT-X (FT8/FT4) or fldigi (RTTY, PSK31, CW).
- It's a best guess, shown as "very likely", "likely" or "maybe". Very weak
  signals, heavy noise, or several signals on top of each other can confuse it.
- The tests so far use computer-generated signals, not recordings of real
  radio. Real-world results may need some tuning. Please report anything it
  gets wrong.

## For tinkerers

- See it with made-up signals: `python -m signal_identifier --demo`
- Analyse a WAV recording: `python -m signal_identifier recording.wav`
- Run the tests: `pip install pytest` and then `python -m pytest`
- How it works: `signal_identifier/classify.py` measures each signal's width,
  keying, tone hopping and loudness pattern and matches them against what each
  mode looks like. No internet connection or AI service is used.
