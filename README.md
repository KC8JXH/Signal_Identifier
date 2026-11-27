# IC7300 Rig Interface

Listens to your radio through your computer's sound card and tells you what kind
of signal you're hearing: **CW, SSB voice, FT8, FT4, RTTY, PSK31, SSTV
pictures** (with the SSTV mode, e.g. Martin M1), weather fax,
Winlink-style **ARQ data bursts** (ARDOP, VARA, PACTOR), a steady carrier, or
just noise. It shows the radio's scope (spectrum and waterfall, like the
IC-7300's screen) with each signal labelled.

![modes](https://img.shields.io/badge/modes-CW%20%7C%20SSB%20%7C%20FT8%20%7C%20FT4%20%7C%20RTTY%20%7C%20PSK31%20%7C%20SSTV-blue)

## Getting it running (Windows)

1. **Download this program.** On this GitHub page, click the green **Code**
   button, then **Download ZIP**.
2. **Unzip it.** Open your Downloads folder, right-click the ZIP file, choose
   **Extract All…**, then click **Extract**.
3. **Install and start it.** In the folder that opens, double-click
   **`Start IC7300 Rig Interface`**.
   - If Windows shows a blue "Windows protected your PC" box, click
     **More info**, then **Run anyway**.
   - A black window shows the setup. The first time takes a few minutes:
     if the computer doesn't have Python yet, it's installed automatically.
   - The program is installed in **`C:\SignalIdentifier`** and starts.
   - An **IC7300 Rig Interface** icon appears on the Desktop. From then on, start
     the program with that icon: it opens with no black window at all.
   - The download tidies itself away: a few seconds after the program starts,
     the unzipped folder and the downloaded ZIP are deleted.
   - If the program ever can't start, a message box says why, and the details
     are saved in `C:\SignalIdentifier\signal_identifier_error.log`.

If Python can't be installed automatically (very old Windows 10 without the
"winget" installer), the black window explains how to install it by hand.

**Updating:** download the ZIP again, unzip it, and double-click
**`Start IC7300 Rig Interface`** in it. The new version replaces the old one in
`C:\SignalIdentifier` (settings and recordings are kept), starts, and the
download is deleted again.

## Using it

Everything you set up once is in the **Settings** menu at the top left of the
window: sound input, listening on/off, identifying on/off, the radio
connection, click-to-tune, the band scope options, and UTC or local time.
Each choice is remembered, and listening and the radio connection start again
by themselves next time.

1. In **Settings > Sound input**, pick your radio's sound input. For radios
   with a built-in USB sound card (IC-7300, FT-991A, etc.) it's usually called
   **"USB Audio CODEC"**. If one entry doesn't work, try the same name with
   **[MME]** after it.
2. Choose **Settings > Start listening**. Tune your radio to USB (upper
   sideband) for digital modes, as you would for WSJT-X or fldigi.
3. Watch the **Audio level** bar. It should say **OK**. If it says "Too loud"
   or "Very quiet", adjust the audio level on the radio or in Windows sound
   settings.

Untick **Settings > Identify signals** to hide the mode labels and just watch
the scope; tick it again to bring them back.

**Settings > UTC time / Local time** chooses what the clock above the
frequency shows. Local time comes from the computer's own clock.

Results update every second, based on the last 15 seconds of audio. FT8 is
easiest to recognise after a full 15-second cycle.

## Showing the radio's frequency (IC-7300 and others)

The program can read the dial frequency, mode and S-meter from your radio.
With that you get:

- the time (UTC or local), frequency (laid out like the IC-7300's, **14.074.00**) and mode
  in the top right corner
- an **S-meter** bar next to the audio level
- **Power, SWR and ALC** meters while you transmit (Icom radios and Hamlib
  rigctld), with a red **TX** lamp. See "Transmit meters" below.
- on Icom radios, the radio's **scope** (spectrum and waterfall), see below

It only transmits when you press **Tune** (see below), and then the radio
itself runs its tune just as if you'd held its TUNER button. The only things it ever changes
on the radio are the frequency and mode, and only when you click a signal (with
"Click to tune radio" ticked) or press a band or mode button. On Icom radios it
also switches the radio's scope on and sets its span when you ask (see
"Keep scope on" below).

**IC-7300, step by step**

1. Connect the radio to the computer with the USB cable and turn the radio on.
   (If Windows doesn't recognise it, install the **Icom USB driver** from
   Icom's website.)
2. In **Settings > Radio** choose **Icom IC-7300**.
3. In **Settings > Port**, pick the entry that says **Silicon Labs CP210x …
   (COMx)** (it's usually picked for you). If the list is empty, choose
   **Refresh the list** at its bottom.
4. Leave **Settings > Speed** at **115200**. On the radio, go to **MENU → SET →
   Connectors → CI-V** and set:
   - **CI-V USB Baud Rate**: **115200**
   - **CI-V USB Port**: **Unlink from [REMOTE]** (needed for the scope)
5. Choose **Settings > Connect radio**. The frequency and mode appear in the
   top right.
   Next time it reconnects automatically.

Also supported: other Icoms (IC-705, IC-7610, IC-9700, IC-7100, …), Yaesu
(FT-991A, FT-891, FT-710, FTDX10/101, FT-817/818/857/897), Kenwood and
Elecraft. Set **Settings > Speed** to match the CAT rate set in your radio's menu.

### The scope (Icom radios)

The big display is the radio's own scope, laid out like the IC-7300's screen:
the **spectrum** on top (signal strength across the span, with a dotted grid of
10 divisions) and the **waterfall** below it. The white ▼ and line mark the
frequency you're on, and the shaded area is what you're hearing (the receive
passband). The marker turns red while you transmit. Signals the program has
identified are labelled at their frequency, just above the spectrum.

- **Span tick boxes** (±2.5k, ±5k, ±10k, ±25k, ±50k, ±100k, ±250k, ±500k), next
  to the band buttons, set the radio's scope to Center mode with that span,
  just like its SPAN key. The box for the span the radio is showing is ticked,
  even if you change it on the radio. In the radio's Fixed mode no box is
  ticked and the program shows whatever the radio shows.
- **Click** anywhere in the scope and the radio goes to that frequency, like
  touching the radio's screen (to the nearest 10 Hz in narrow spans, 100 Hz in
  medium ones, 1 kHz in the widest; AM to 1 kHz and FM to 5 kHz steps).
- Untick **Settings > Click to tune radio** if you don't want clicks to tune.
- Signals are identified from what you're hearing, so a signal gets its label
  once it's inside the shaded passband.
- **NO SIGNAL** appears in the middle of the scope when the radio isn't
  sending scope data, for example when its scope is switched off or a menu is
  open on the radio's screen.
- **Settings > Keep scope on** (ticked by default): if the scope data stops,
  the program asks the radio to switch its scope back on every 5 seconds.
  Untick it if you want to switch the radio's scope off yourself and leave it
  off. While a menu is open on the radio, the radio sends no scope data at
  all, so the scope shows NO SIGNAL until you close the menu.

**Using WSJT-X at the same time?** Only one program can use the radio's COM
port at once. To share it, run **flrig** (free), point both WSJT-X and this
program at it (choose **flrig** in the Radio list), or use **Hamlib rigctld**.

Identified signals are placed at their on-air frequency in USB, LSB, data
modes, CW and RTTY (in CW using the IC-7300's CW pitch, read automatically;
other radios assume 600 Hz). In AM and FM the label goes on the station you're
tuned to.

The S-meter uses Icom's published scale for Icom radios and rigctld's
calibrated reading. For other radios it's approximate.

### Meters and filters

- **Analog meter** (left of the mode buttons): a needle S-meter on receive
  (S1 … S9, +20 … +60 dB in red). While you transmit it turns red and becomes a
  power meter in watts (full scale 100 W, or 10 W for the IC-705 and IC-905).
  Hide or show it with **Settings > Analog meter**.
- **FIL1 / FIL2 / FIL3** choose the radio's receive filter for the mode you're
  in; the one in use lights up, also when you change it on the radio. (USB-D
  and LSB-D have their own filter setting on the IC-7300, as on the radio.)
- **Transmit bars**, under the S-meter, show the radio's own meters while it
  transmits:
  - **Po**: output power in watts.
  - **SWR**: turns amber at 1.5 and red at 2.0. The shaded part is above 2:1.
  - **ALC**: 0–100 % is inside the radio's ALC zone. **OVER** means you're
    overdriving: turn down the USB MOD level on the radio, or the power/audio
    slider in your digital-mode program, until ALC stays in the zone (for
    FT8, ideally close to zero).
  - **Id**: the power amplifier's current in amps (about 21 A at 100 W on an
    IC-7300).
- The IC-7300 has no temperature reading (not on its own screen, nor over
  CI-V), so there's no temperature meter.

The white tick on each bar holds the peak for a moment. These meters work on
Icom radios and through Hamlib rigctld; for other radios they show "n/a". The
Icom scales follow Icom's published CI-V figures, so it's worth comparing them
once with the radio's own meters.

## Recording

Press **● Record** to start recording the receiver audio, and **■ Stop rec**
to finish. The button shows how long you've been recording. The file goes in
the `recordings` folder (the folder opens when you stop), named with the time
(UTC) and, if the radio is connected, its frequency and mode.

Recordings of signals the program gets wrong or can't identify are the best
way to improve it. To share one, open this project on GitHub, click
**Add file → Upload files**, drag the WAV file in and click **Commit changes**.

You can also analyse a saved file: `python -m signal_identifier recordings\yourfile.wav`

## Tune button

**TUNE** (under the mode buttons) runs the radio's automatic antenna tuner,
exactly like holding the **TUNER** button on the radio: the radio sends a
low-power carrier, the tuner finds a match, and the radio goes back to receive
by itself. The button shows **TUNING…** while that happens, then **TUNED** or
**NO MATCH**, and the status line gives the SWR. The Power and SWR meters show
the tune carrier while it's on the air.

- This is the only time the program makes the radio transmit, and only when
  you press the button. Make sure you're on a ham band and the frequency is
  clear before tuning.
- Works on Icom radios with an internal tuner (IC-7300, IC-705 with its tuner,
  IC-7610, IC-9700...) and through Hamlib rigctld. Tuning stops on its own
  after 20 seconds if the radio doesn't report back.

## Changing frequency from the display

With the radio connected:

- **Click the frequency** in the top right, type a new one and press **Enter**
  (**Esc** cancels). Any of these work: `14.074`, `14074`, `14074000` or the
  radio's own style `14.074.00`.
- **▲ / ▼** next to the frequency step it up or down. Hold one down to keep going.
- The mouse wheel over the frequency does the same.
- Click **step** under the frequency to choose the step size: 10 Hz, 100 Hz,
  1 kHz or 5 kHz. Steps land on round numbers: from 14.074.03, ▲ with 100 Hz
  steps goes to 14.074.10.

## Band and mode buttons

With the radio connected:

- **Band buttons** (160 … 6, including 11 m for CB) above the scope change band. Each band remembers
  the frequency you were last on (the first time, its phone spot; on 30 m, the
  FT8 spot). Pressing the band you're already on jumps back to that spot.
- **Every band starts in its default mode**: USB from 30 m up to 6 m, LSB below
  30 m, except 60 m, which is USB (voice there is USB-only), and 11 m (CB,
  26.965-27.405 MHz), which is AM and starts on channel 19 (27.185 MHz). Going below
  1.7 MHz (the AM broadcast band) switches to AM, whether you type the
  frequency, click the scope or step with the arrows; moving onto another band
  that way also switches to that band's default. Once you're on a band,
  change the mode on the radio or with the buttons: the buttons always show
  what the radio is actually on. Switching band goes back to the default.
- **Mode buttons** (LSB, USB, CW, RTTY, AM, FM), next to the frequency
  display, change mode. **DATA** turns data mode on or off for the sideband
  you're on (USB ↔ USB-D, LSB ↔ LSB-D), like the radio's DATA key. The
  current band and mode light up (USB and DATA together in USB-D).

## SSTV pictures

SSTV (slow-scan TV, e.g. around 14.230 MHz) is labelled with its mode, for
example **SSTV Martin M1**. Recognised: Martin M1/M2, Scottie S1/S2/DX,
Robot 36/72, PD50/90/120/160/180/240 and Wraase SC2-180. Weather fax is
recognised by its line rate too.

It's found from the steady rhythm of the picture's scan lines (Martin M1
sends one every 446 ms), so it works even if you're tuned off frequency.
If you are, the label tells you which way to go, for example
**SSTV Martin M1 - tune down 1.0 kHz**. Tune that much and the picture lands
on the normal tones (1500-2300 Hz) that MMSSTV or QSSTV expect. The program
needs about 6 seconds of picture before it's sure.

## About "ARQ data" labels

Winlink-type stations (ARDOP, VARA, PACTOR) send data in bursts lasting from a
fraction of a second to several seconds, taking turns with the other station.
The program labels these **ARQ data** with their width, and **ARDOP** when it
sees ARDOP's 4 tones 50 Hz apart. It can't yet tell VARA from PACTOR, and a
2,300 Hz-wide signal that fills the whole filter is still hard for it.

## Good to know

- The program lives in **`C:\SignalIdentifier`**: don't delete that folder
  (the Desktop icon points into it). Recordings are saved in its
  `recordings` folder. To install somewhere else, change the `INSTALL_DIR`
  line near the top of `Start IC7300 Rig Interface.bat`.
- To remove the program completely, delete `C:\SignalIdentifier` and the
  Desktop icon.
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
