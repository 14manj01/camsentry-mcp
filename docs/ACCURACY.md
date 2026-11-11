# Accuracy: what was measured, and how far to trust it

Everything here was measured while building version 0.2 on one PC, with small test sets labelled by
looking at each image. It is enough to choose between prompts and models and to show where the weak spots
are. It is not a benchmark, and your cameras will differ.

## The test sets

| Set | What it is |
| --- | --- |
| **Street stills** | 15 real 640×360 event stills from one street-facing Wyze Solar Cam Pan. 5 show a person (one of them a pedestrian a few pixels tall on the far side of the street), 10 show none. 2 show a dog. Passing cars triggered most of them. |
| **Public photos** | 8 photos with obvious content: a bus with pedestrians, two men, a dog, two cats, two parcels on doorsteps, a courier handing over a box, a parked car. 29 present-or-absent facts about people, pets, vehicles and packages. |
| **Statements** | 72 statement/image pairs over both sets, 18 true and 54 false, such as "a person is walking a dog" or "the door is open". |

## Detection with the default model

`qwen2.5vl:3b` (3.2 GB), the complete pipeline including the second look described below:

| | Result |
| --- | --- |
| Street stills: real people reported | 4 of 5. The miss is the distant pedestrian. |
| Street stills: people reported where there were none | 0 of 10 |
| Street stills: pet present or absent | 15 of 15 right |
| Public photos: present-or-absent facts | 29 of 29 right |
| Time per still | about 0.7 s, with the model already loaded on a GPU that fits it |

Counts are less reliable than presence: the bus photo shows four pedestrians and was reported as three.

## Why a person or package needs a second look

Small models decorate street scenes. Shown a pickup truck driving past, they write "a man is standing near
the truck"; shown two parked cars, "a mail truck delivering packages". The first look alone, on the 23
images of the first two sets, reported a person in 3 of the 15 images that had none.

So a person or a package is only reported when a second request, asked about nothing else, also finds one.
With it: no phantom person in those 23 images, at the price of the one distant pedestrian. For packages it
kept both real parcels, dropped the one phantom parcel seen during live testing, and dropped the box the
courier was still holding.

The one-sentence description is not corrected. It can still mention a person the second look rejected;
alerts and counts follow the second look, not the sentence.

## Model comparison on the street stills

| Model | Size | Real people found | People invented | Pets right | Unanswered | Time per still |
| --- | --- | --- | --- | --- | --- | --- |
| `qwen2.5vl:3b` | 3.2 GB | 4 of 5 | 0 of 10 | 15 of 15 | 0 | 0.7 s |
| `gemma3:4b` | 3.4 GB | 5 of 5 | 10 of 10 | 12 of 15 | 0 | 1.6 s |
| `qwen3-vl:4b` ¹ | 3.3 GB | 3 of 3 answered | 1 of 10 | 13 of 13 answered | 2 of 15 | 6.6 s |

¹ Measured with an earlier version of the second look. It is a thinking model: it reasons at length about
each still even when asked not to, which makes it slow and sometimes fills its context before answering.

`gemma3:4b` reported a person in every street still, with or without the second look. That is why the
automatic choice prefers `qwen2.5vl:3b` and uses `gemma3:4b` only when nothing better is installed.

## Watch rules

Watch rules are the least reliable part. Each statement is judged by asking the model to list what it can
see that makes the statement true; the rule counts as true when the list is not empty.

| Way of asking | Right | Said yes to a false statement | Said no to a true one |
| --- | --- | --- | --- |
| List the visible evidence (used) | 55 of 72 | 17 of 54 | 0 of 18 |
| Describe the scene, then answer yes or no | 45 of 72 | 27 of 54 | 0 of 18 |
| Both must agree | 56 of 72 | 16 of 54 | 0 of 18 |

A true statement was never missed, but about three in ten false ones were called true, and requiring two
differently-worded judgements to agree did not help. Rules about large, obvious things ("a car is parked",
"a cardboard box is on the floor") did well; rules about small things in a wide street view ("a person is
walking a dog") did not. A rule alerts when it becomes true and not again while it stays true, so a rule
that is wrongly true for a whole scene costs one alert, not a stream of them.

## Faces

A face detector (OpenCV YuNet) was run over 55 real stills from five cameras. It found no face in any of
them. The stills are 640×360, or 480×684 from the doorbell, so a face a few metres from the camera is only
a handful of pixels wide. Recognising familiar faces is not possible on these images, and is not built.

## What this means in practice

- "Is there a person / pet / package?" is usable, and errs on the side of not inventing people.
- Someone far from the camera can be missed.
- "Lingering" means a person was in consecutive stills. It cannot tell one person staying from two
  people passing a few minutes apart.
- "Package taken" means a package was listed in one still, not in the next, and a person was seen around
  then. A package the model simply failed to see a second time looks the same.
- Treat every alert as a reason to look, not as a fact.
