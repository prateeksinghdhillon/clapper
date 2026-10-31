# Clapper prompt guide

How to write scenes, references, dialogue and sound so MiniMax H3 does what you mean. The rules come from MiniMax's H3 prompt guides, the ComfyUI-H3-Continuum author's prompt skill, and what worked (and didn't) in real Clapper renders. Where something is a practice rather than a proven rule, it says so.

## 1. What the model receives

For every take, Clapper assembles one prompt in this order:

```
<style text>                       ← same for every scene
subject_definitions:               ← inserted here if your style text has this line,
<definitions of the references        otherwise a block is added at the top
 this scene uses, in number order>
Continuation of Chunk N.           ← added automatically for Continue/Bridge scenes
<scene text>
```

and replaces every `@name` with the number the model knows it by in *this* scene (`<Picture 2>`, `<Audio 1>`). You never type numbers. Open **Prompt sent to H3** in the scene editor to see the exact result before rendering.

## 2. The style text

The style text is sent with every scene. Put only things that are always true in it:

- **Format and flow**: "A 50-second vertical 9:16 animated video, told as one continuous flowing sequence. Every change of place happens through a smooth, motivated camera move; the camera never cuts."
- **Look**: "2D flat cartoon animation drawn in the style of @style, clean, smooth natural motion, stable lighting and colours from start to end." Describe the look once — repeating intensity words (vivid, glossy, ultra-sharp) in every scene can make long videos drift.
- **Voices**: "(S1) is the only speaking voice: an off-screen male narrator with a warm, friendly voice, the same in every scene. Whenever (S1) speaks, every visible character keeps their lips closed."
- **Rules**: "Signs, papers and screens show only pictures, icons and plain lines, with no readable writing."
- A line `subject_definitions:` where the reference definitions should go (optional).

Do **not** put one-time actions, dialogue, or the ending in the style text. Once takes exist, avoid editing it: every change changes every scene's prompt.

## 3. References

### Naming and defining

Each reference has a short `@name` and a **definition** written for the model:

```
@opening is the opening frame. It defines the café and the two recurring characters:
the owner, a woman in her 30s with short curly hair and a green apron, and the
barista, a tall young man in a black cap. These two keep the same faces and clothes throughout.

@hero is the mascot: a round orange fox with big green eyes and a white chest.
Whenever the fox appears, it is exactly this character.

@style is a style sheet only. It defines how every person is drawn: thin dark outlines,
flat colours with soft shading, simple friendly faces. The people shown in @style are
style examples and never appear in the video.

@logo is the logo: <describe shape, colours and the exact spelling>.
It appears only in the final scene, exactly as in @logo.
```

- Say **what the reference contributes** (a face, an outfit, a style, a place) and what it must not contribute ("its people never appear").
- Only describe what must survive into the shot; full-body detail can bleed into close-ups.
- A definition is included only in scenes that use the reference, so a scene without `@logo` carries no logo definition.

### Start image

The start image is the first frame of scene 1 and is shown to the model as `<Picture 1>` in every scene. Refer to it in the definitions and in scene 1 ("The video opens on the composition of @opening"). Later scenes continue from the previous take, not from the start image — never write "return to the opening frame" in a later scene.

### Which references a scene uses

- By default a scene uses the ★ project defaults. Give a scene its own list when some references are not needed — fewer references mean slightly faster renders and less temptation for the model to show things too early (a logo reference in every scene pulls the model toward drawing the logo).
- If the text uses a `@name` that is not active in the scene, Clapper refuses to render and says which one.
- Limits per scene: 9 images, 3 audio references. Pre-scale large images to about 0.3 megapixels for memory.
- Removing `@logo` from a scene does not stop the model from **writing the brand name** if the scene text talks about the brand. Keep brand names out of on-screen descriptions (see §7).

### Voice references (audio)

Add a clean voice sample as an audio reference and map it to a speaker in the style text or the scene: "(S1) is the narrator, voice from @narrator." The sample guides timbre and delivery; the generated sound is still the model's own. The mapping is an instruction, not a guarantee — with several voices the model can mix them up. **Not yet tested in Clapper renders.**

## 4. The scene text

Use H3's three fields:

```
integrated_multimodal_description: [Shot 1] <visible action, camera, dialogue>
overall_soundscape: <ambience, action sounds, non-verbal human sounds>
non_diegetic_music: <music only the audience hears, or N/A>
```

A first line before the fields can state the incoming state ("The camera is on the barista at the counter.").

### Action

- Write **concrete, visible** things: the current pose and position, then the next action and its visible result.
- Order: use **then** or a new sentence for things that happen one after another, **while** / **meanwhile** for things at the same time. A bare *and* is ambiguous.
- Give rough timing when it matters: "At about 1.5 seconds, …", "In the final second, …". It is soft timing, not frame-accurate.
- Use precise motion verbs (bounce, sway, swing, wobble); avoid piling up synonyms.
- Left/right means the camera's left/right unless you say "to her left".

### Camera

State the shot size (close-up, medium, wide), angle (eye-level, low, overhead) and movement (pan, tilt, push-in, pull-back, dolly, orbit, follow), plus speed when it matters: "Medium shot, a slight push-in on the gasp, then one continuous pan right."

`[Shot 1]` describes one continuous camera shot. Only use `[Shot 2]` for an intended cut inside a scene. A new scene does **not** mean a cut.

### Start from where the previous scene ended

This is the most important continuity rule. The new take begins exactly where the previous take ended. If the scene text opens with a *different* state, the model often jumps straight to it on the first new frame.

- ✗ "The phone screen shows the chat with two new messages." (the previous scene ended on a product catalogue — the screen will snap)
- ✓ "The catalogue on the phone slides away and the chat reappears with two new messages."

End each scene in a state the next scene can pick up ("In the final second the camera pans right to the counter, where the phone starts ringing"), and start the next scene from it.

What counts is how the previous **take** ends, not what its text asked for: look at its last frames before writing the next scene (in Clapper the assistant uses `view_take` with `part: end`). If the take ended with something unexpected filling the frame — a vehicle pulling up close, a character turned away — start from that.

### One action per scene, staged for the camera

*From one tested case (a character boarding a bus): the original text, with five beats, the camera inside and the friend in the references, gave three takes in which she seemed to get off and turn back; the same scene rewritten by these rules boarded correctly on the first try, with the friend in the references and without. Treat them as strong guidance, not as proven rules.*


- **One main action per 5 seconds**, plus at most a small reaction. "She walks to the door, climbs in, stands in the aisle, looks back and sees him smile" is five beats; the model compresses them and skips to the last one. Split it: scene A boards the bus, scene B finds him.
- **Say where the camera is and which way people move**, especially when someone enters or leaves something. To get on a bus, keep the camera *outside* and *behind* her: "the camera stays on the platform behind her; she climbs the steps into the bus, her back to the camera, moving away from us". If the camera is inside, the model shows her facing the camera in the doorway — which reads as getting *off* — and then turns her round.
- **Only give a scene the references that should be visible in it.** A character in the references tends to be shown early and large; a friend who is only discovered at the end of the boarding pulls the camera inside the bus. Leave them out of that scene, or bring them in with "in the final second, … is glimpsed through the door".
- **Name the incoming state in the first line** when the previous take ends on something big ("The bus is pulling in beside the shelter, its side close to the camera.") and let the action grow from it.

## 5. Dialogue and narration

- Write spoken words as `<d>[Language] exact words</d>` and always say **who** speaks: "the off-screen narrator (S1), excited, says: `<d>[English] Now there's an easier way.</d>`"
- Keep the language tag and the words exactly as they should be spoken. Write non-Latin languages in their own script.
- **Off-screen narrator:** say it is off-screen and that visible characters keep their lips closed (in the style text once, and in the scene if needed).
- **Density:** keep it to what fits comfortably in the scene — roughly one or two short sentences per 5 seconds. Too much speech gets rushed or cut off. (A guideline from practice, not a measured limit.)
- Put each line in only one scene; don't repeat dialogue across scenes unless intended.
- Pronunciation quality in non-English languages varies; judge it on the first scene.

## 6. Sound and music

- **overall_soundscape**: ambience, action sounds (a plate hitting a table, a door bell), and non-verbal human sounds (gasp, laugh, yawn). It also influences the picture, so keep it consistent with the action.
- **A clear "hero" sound early in the scene** — one loud, specific sound in the first second or so, described in `overall_soundscape`, with the narration starting after it ("At about 1.5 seconds…") — has worked well for keeping sound effects from being drowned out by narration. *Practice, not a proven rule.*
- **non_diegetic_music**: audience-only music; `N/A` when there is none. Describe tempo and instruments and keep them continuous across scenes ("The same playful score continues, slightly faster"). Don't restart an intro in every scene; let music resolve only at the end.

## 7. Text, screens and logos

Video models draw text poorly: letters warp and spelling drifts.

- Describe signs, papers and screens as **pictures, icons and plain lines** with no readable writing.
- The model may still write words that appear in the scene text (for example the product or brand name). Keep such words out of descriptions of what is *visible*; they are fine inside dialogue.
- For a logo that must be exact, show it only in the scene that needs it, with the logo as a reference and its definition spelling it out. (A planned *overlay* feature will composite a real logo image in the export instead.)

## 8. Worked example (generic)

**Style text**

```
A 30-second vertical 9:16 animated promo for a neighbourhood café, one continuous flowing
sequence; the camera never cuts.

subject_definitions:
(S1) is the only speaking voice: an off-screen female narrator with a bright, friendly voice.
Whenever (S1) speaks, every visible character keeps their lips closed.

Style: 2D flat cartoon animation in the style of @style, warm palette, smooth natural motion,
stable lighting. Menus, signs and screens show only pictures, icons and plain lines, no readable writing.
```

**Scene 1** (references: defaults — `@hero`, `@style`; start image `@opening`)

```
integrated_multimodal_description: [Shot 1] The video opens on the composition of @opening.
The barista slides a cup across the counter; the cup tips and coffee splashes over the order
slips, and the owner's eyes go wide. At about 1.5 seconds, the off-screen narrator (S1) says:
<d>[English] Another mixed-up order?</d> In the final second the camera pans right to the
window, where an orange streak of light is flying toward the café.
overall_soundscape: In the very first second, a loud clatter as the cup tips and a splash;
café chatter and a hissing espresso machine underneath.
non_diegetic_music: Light, playful pizzicato strings, no vocals.
```

**Scene 2** (Continue — Clapper adds "Continuation of Chunk 1.")

```
The camera is on the window with the orange streak approaching.
integrated_multimodal_description: [Shot 1] The streak bursts through the open door and becomes
the fox from @hero, landing softly on the counter beside the owner. The fox smiles and taps the
counter; a tidy row of order icons appears above it in a burst of sparkles. The owner laughs.
At about 1.5 seconds, the off-screen narrator (S1), excited, says: <d>[English] Meet your new
order helper.</d> A slight push-in on the fox, then the camera settles.
overall_soundscape: In the very first second, a whoosh as the streak bursts in, then a soft
landing thump and a sparkle chime.
non_diegetic_music: The same playful strings, now joined by light percussion.
```

## 9. Checklist before rendering

- [ ] **Prompt sent to H3** shows no error, and every `@name` became the right number.
- [ ] The scene starts from the state where the previous **take** ends (looked at, not assumed), and ends in a state the next scene can use.
- [ ] One main action; the camera position and the direction of movement are stated when someone enters or leaves something.
- [ ] Only the references that should be visible in this scene.
- [ ] Every spoken line has a speaker and a `[Language]` tag; the amount of speech fits the length.
- [ ] Sounds are in `overall_soundscape`, music in `non_diegetic_music` (or `N/A`).
- [ ] No readable text is requested except where it is the point (and then with its reference).
- [ ] The scene uses only the references it needs.
- [ ] Render one take of a new kind of scene before rendering many.
