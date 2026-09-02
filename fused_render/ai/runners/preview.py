"""The live preview: the picture WHILE it is being denoised (SPEC §40).

`fused.ai.image` writes its PNG once, at the end. A FLUX render is minutes long
and for all of them the only thing a page can show is a step counter — a
progress bar for a process whose entire output is visual. This module is the
fix: a `<out without ext>.preview.png` beside the image the request already
named, ~32x32 and ~3KB, **overwritten every step**, which a page points an
`<img>` at. Blurring and upscaling it is the page's business, not this API's.

**Always on, no flag.** Measured at 68ms per step for the projection plus the
PNG at 512²/16 steps — 1.25% of the render — and dominated by the device->CPU
sync and the PNG encode rather than by the matmul, so it stays roughly flat as
the resolution grows. A feature that costs one percent does not need an opt-in;
an opt-in would only guarantee that most pages never get it.

**This module is the whole of the feature and it sits at the runners ROOT**,
beside `worker_base.py`, `formats.py`, `diarize.py` and `partial.py`, for the
reason AI-10c states: two engines serve one capability and a page must not be
able to tell which one ran. The path rule, the arithmetic, the write and the
lifecycle exist once, and `tests/test_ai_image_preview.py` pins that
structurally — a second `preview.py` under either image runner would fail no
behavioural test, because both copies would pass their own.

**Stdlib only at import time, and no import of `fused_render`.** The same
constraint `formats.py`, `diarize.py` and `partial.py` document, for the same
two reasons: each runner runs on its own interpreter with the app's package off
its path, and the SERVER imports this file too — to derive the path it
advertises to the page — so reading it must not need either runner venv. numpy
and Pillow are both present in both image venvs (torch/mlx pull the first,
`pillow`/`mflux` the second) and both are used, inside the functions that need
them.

---

## Why a model's `estimate` strategy matters

A denoising loop's raw latent is not always the picture. FLUX.2 klein is
step-wise **distilled**, and its sigma schedule is nothing like a normal
flow-matching one. A real 16-step render walked

    1.0, 0.991, 0.98, 0.968, 0.955, 0.939, 0.921, 0.9, 0.875, 0.845,
    0.808, 0.761, 0.7, 0.617, 0.5, 0.318, 0.0

— still at sigma 0.5 with two steps to go. What the denoising callback holds is
therefore mostly noise for almost the whole render, and projecting it gives a
preview that is grey static until the last frame, which is the frame nobody
needed. What IS legible from step 2 of 16, and converges visibly to the finished
image, is the model's own current guess: recover the velocity from two
consecutive latents and extrapolate it to sigma 0.

    v      = (x_next - x_prev) / (s_next - s_prev)
    x1_hat = x_next - s_next * v

klein's entry in `PROJECTIONS` therefore carries `"estimate": "extrapolate"`.
Step 1 has no predecessor and so gets no preview under this strategy — that is
correct rather than a gap to paper over, because a first frame projected from
the raw latent is exactly the static extrapolation exists to avoid.

An ordinary, non-distilled sigma schedule does not have that problem: its raw
latent is legible from early on, which is how every other UI previews a normal
diffusion model. An entry for a model like that carries `"estimate": "raw"`,
and the sink projects the latent it is handed directly, with no predecessor
needed — so step 1 DOES get a frame, unlike an extrapolating entry's gap.

## Why the projection is a fitted constant, and why it is per-model

Each entry's `factors`/`bias` pair is a fitted affine map from that model's own
packed latent-token space to sRGB, and nothing about the map generalises across
latent spaces: FLUX.1's published 16-channel latent-RGB factors do not apply to
klein, and klein's 128-wide matrix does not apply to a 4-channel VAE either. The
channel width a `project()` call uses comes from the entry's own `factors`, not
from a module constant, which is what lets one code path serve every fitted
model regardless of its channel count.

klein's VAE is `AutoencoderKLFlux2`: 32 latent channels, a 2x2 patchify (so 128
channels per token), 8x spatial (so `"stride": 16`), and a normalisation that is
a **BatchNorm** (`vae.bn.running_mean` / `running_var` +
`vae.config.batch_norm_eps`) rather than the usual `scaling_factor`/
`shift_factor` pair. A denoising callback holds `(1, H*W, 128)` row-major
packed tokens with a grid side of `image side / 16`.

klein's map was fitted, once, by `fit_factors.py`: encode the three sample jpgs
that ship in the FLUX.2 repo through the torch VAE, patchify and BN-normalise to
land in **exactly the space the callback sees**, box-downsample each source
image 16x to one pixel per token, and least-squares a 128->3 affine map between
them. R² = 0.911 / 0.912 / 0.891 per channel, residual RMS 0.083 in [0,1]. The
numbers are written down here rather than derived at runtime because deriving
them needs the VAE weights, which is the one thing a preview must not wait for.

Stable Diffusion 1.5's map was fitted the same way, and the method is the part
worth copying for a third model: five 512² images through the model's own
`AutoencoderKL`, scaled by the config's `scaling_factor` (0.18215) so the fit
lands in the space the callback holds, box-downsampled 8x to one pixel per
token, least-squares over the 20,480 tokens that gives. R² = 0.885 / 0.923 /
0.876, residual RMS 0.097. Four channels rather than 128, no patchify — hence
`"stride": 8` — and an ordinary schedule, hence `"estimate": "raw"`.

**One matrix serves both engines, per entry.** `bn.running_mean` and
`bn.running_var` are bit-identical (max|diff| = 0.0) between
`black-forest-labs/FLUX.2-klein-4B` and `mlx-community/FLUX.2-Klein-4B-4bit`,
and mflux's `decode_packed_latents` applies the same `packed * bn_std + bn_mean`
with the same unpatchify permutation that diffusers' id-scatter produces for
text2image. That is why the table below is keyed by the VAE's class name and
not by the repo id: the two runners reach the same row from opposite directions
(`type(pipe.vae).__name__` on one side, `formats.MFLUX_VARIANTS[...]["vae"]` on
the other), and a model whose latent space nobody has fitted gets **no preview
at all** — a working no-op sink, so a render without an entry behaves exactly as
it did before this module existed. That is what keeps this additive.
"""

from __future__ import annotations

import math
import os

#: What replaces the render's `.png`. A SIBLING of the image, sharing its stem:
#: `20260101-120000-abc.png` and `20260101-120000-abc.preview.png` sort together
#: in `ai/images/`, which a user browses, and neither reads like the other.
#: Appending (`out.png.preview.png`) would leave a name that ends in the same
#: extension twice and looks like a second render.
SUFFIX = ".preview.png"

#: The longest side a frame is written at. A 512² render has a 32x32 token grid,
#: a 1024² one 64x64 and a 2048² one 128x128 — and the cost that was measured is
#: the cost of a 32x32 PNG. Capping keeps the per-step price flat across
#: resolutions, and costs nothing a viewer can see: the page is upscaling and
#: blurring a picture of a picture either way.
MAX_SIDE = 32

#: Consecutive failed frames before a sink switches itself off.
#:
#: A preview never costs a render (see `Sink.add`), so a failure is swallowed —
#: but a PERMANENT failure swallowed silently is a device sync plus a doomed
#: syscall on every one of a hundred steps. Three in a row is the line: the
#: failure this most expects is a Windows lock held by the page's own `<img>`
#: over the destination, which is transient by nature, and a sink that gave up
#: on the first unlucky step would throw the feature away over nothing. The
#: count resets on any frame that lands.
MAX_FAILURES = 3


def token_grid(model_key: str | None, width, height) -> tuple:
    """The `(h, w)` token grid a render of `width` x `height` denoises in,
    for `model_key`'s entry in `PROJECTIONS`.

    Both workers know their pixel size and neither can read the grid off the
    packed `(B, N, C)` tokens, so the division lives here — a runner doing its
    own `// stride` is a second copy of a fact about the VAE. The stride is
    `PROJECTIONS[model_key]["stride"]` rather than one constant because it is a
    fact about that model's autoencoder — klein's 8x VAE plus 2x2 patchify is
    16, an unpatchified 8x VAE like SD1.5's is 8 — and a second entry with a
    different packing must not silently reuse the first one's number.

    A `model_key` absent from `PROJECTIONS` returns a grid nobody will ever
    read: both callers compute this before they know whether their sink is
    `wanted`, and an unfitted model's no-op sink discards `grid` unopened. `1`
    keeps the division defined rather than teaching this function to guess at
    a stride nothing has measured.
    """
    entry = PROJECTIONS.get(model_key or "")
    stride = entry["stride"] if entry else 1
    return (int(height) // stride, int(width) // stride)


def preview_path(out: str | None) -> str | None:
    """Where the live preview for `out` goes, or None if there is no out.

    Derived in ONE place because three parties have to name the same file: the
    route advertises it, the worker writes it, and `runtime.js` builds the URL
    a page's `<img>` points at. A page is never asked to string-munge one path
    out of another — the rule `partial.partial_path` already follows.
    """
    if not out:
        return None
    return os.path.splitext(out)[0] + SUFFIX


#: FLUX.2 klein's fitted map, exactly as `fit_factors.py` recorded it: three
#: rows of 128 weights (R, G, B) and the three constants. Written out as a
#: literal because deriving it needs the VAE weights, which is the one thing a
#: preview must not wait for. See the module docstring for how it was fitted.
_FLUX2_FACTORS = (
    (
        0.004319765605032444, 0.006642758846282959, -0.0007230047485791147,
        0.00039216605364345014, 0.0012196688912808895, -0.0032893787138164043,
        -0.0023591439239680767, -0.0008559596026316285, -0.0028271269984543324,
        -0.007499493192881346, -0.002115743001922965, -0.0010739217977970839,
        0.014385404996573925, 0.03257348760962486, 0.028830809518694878,
        0.03811405226588249, 0.007008147891610861, 0.001040852046571672,
        0.0034754264634102583, 0.0020419186912477016, 0.0014182536397129297,
        0.00017251365352422, 0.0003846861363854259, -0.0004692572692874819,
        -0.006217387039214373, -0.0059965914115309715, -0.005683690309524536,
        -0.002909251255914569, -0.0026819377671927214, 7.079380156937987e-05,
        -0.005395818967372179, -0.0052736373618245125, -0.05788913741707802,
        -0.05462039262056351, -0.056071482598781586, -0.04536673054099083,
        -0.00048514496302232146, -0.0018123873742297292, -0.005414324812591076,
        -0.003954681102186441, 0.0017698599258437753, 0.0008416266064159572,
        0.0006753841880708933, 0.0025590350851416588, 0.005032898858189583,
        0.005456556100398302, 0.0020204943139106035, 0.004936059936881065,
        0.0010978523641824722, 0.0009927782230079174, 0.0022764126770198345,
        -0.0005715248407796025, -0.0019375573610886931, -0.0010985295521095395,
        -0.00045477927778847516, -0.002286893781274557, 0.00263522588647902,
        -0.0009740614332258701, 0.0011853392934426665, -0.0018170623807236552,
        -0.01178462989628315, -0.007736557628959417, -0.011570101603865623,
        -0.008714784868061543, -0.00375718274153769, -0.003101083217188716,
        0.002798135858029127, -0.003109609941020608, 0.0022560900542885065,
        -0.001226974418386817, 0.0012127363588660955, 0.000674975395668298,
        -0.005696927197277546, -0.0030896333046257496, -0.0019271587952971458,
        -0.004249103367328644, 0.011104568839073181, 0.010736517608165741,
        0.013935495167970657, 0.009387445636093616, -0.0006810142658650875,
        0.0004111784801352769, 0.0002535065868869424, -0.00015048008935991675,
        0.0014298794558271766, 0.00018491005175746977, -0.0012072670506313443,
        -0.0033697609324008226, 0.0008210957748815417, 0.001347901183180511,
        -0.0009010171052068472, 0.0006450305809266865, 0.0025541384238749743,
        0.0021683203522115946, -0.0012691175797954202, -0.00022639325470663607,
        -0.002680952427908778, 0.0013456307351589203, -0.0024516519624739885,
        0.0018459331477060914, 0.0007122131646610796, -0.001650148187763989,
        -0.004450578708201647, -0.0021670707501471043, 0.006447337567806244,
        0.007563515566289425, 0.003611154155805707, 0.004609786439687014,
        0.007319333031773567, 0.0017690816894173622, 0.0009661246440373361,
        7.226940215332434e-05, -0.005029898136854172, -0.00033574795816093683,
        0.0016172690084204078, 0.0031301246490329504, -0.00432027131319046,
        -0.009064449928700924, -0.007024370599538088, -0.004720802418887615,
        0.00028518948238343, -0.00029789458494633436, -0.0003985691873822361,
        0.004343509208410978, 0.0022476192098110914, 0.0011696548899635673,
        0.005508198868483305, 0.002861988265067339,
    ),
    (
        0.0034148988779634237, 0.0070432014763355255, 0.0017529254546388984,
        0.0028275945223867893, 0.0009609725675545633, -0.004111018031835556,
        -0.003459643805399537, -0.0025663108099251986, 0.0021073592361062765,
        -0.0018211830174550414, 0.0013698607217520475, 0.002512849634513259,
        0.03404124826192856, 0.0444403775036335, 0.0446140430867672,
        0.049372103065252304, 0.0018090720986947417, -0.0023069174494594336,
        0.0012175474548712373, 6.446584302466363e-05, -0.002638330915942788,
        -0.00194084201939404, -0.0007935801986604929, -0.0018358816159889102,
        0.007274247240275145, 0.005180122330784798, 0.006793948356062174,
        0.007183389738202095, 0.001261013327166438, 0.0011284986976534128,
        -0.0042471084743738174, -0.0010458165779709816, -0.04735724627971649,
        -0.03988916054368019, -0.04207339137792587, -0.027211442589759827,
        -0.00040315708611160517, -0.0014614894753322005, -0.005677468609064817,
        -0.0036556359846144915, 0.0010590190067887306, 0.0013988933060318232,
        -0.0016817450523376465, 0.0024990320671349764, -0.001685467315837741,
        -0.0005607864004559815, -0.0026659753639250994, -0.000577025581151247,
        0.002216205932199955, 0.004346057306975126, 0.001112748752348125,
        0.00014651997480541468, -0.0021955263800919056, 0.0012995416764169931,
        -0.0026010156143456697, -0.0005221454193815589, 0.005691594909876585,
        0.0023765326477587223, 0.0036872252821922302, 0.0012299439404159784,
        0.0001382525806548074, 0.003853888250887394, -0.003670091275125742,
        0.0010674693621695042, -0.0019302365835756063, -0.0011030228342860937,
        0.004642413463443518, 0.0007383595802821219, 0.00012394941586535424,
        -0.002786841243505478, 0.001386596355587244, 8.776979666436091e-05,
        -0.003786040237173438, -0.000441443087765947, -0.0005074712680652738,
        -0.0047491067089140415, -0.00640726275742054, -0.005853611044585705,
        -0.002752845175564289, -0.004113187547773123, -0.0020386073738336563,
        0.00025124827516265213, -0.0014114718651399016, -0.0023085209541022778,
        0.002601321553811431, 0.001963091781362891, -0.0008846810669638216,
        -0.002873504301533103, 0.0007136394851841033, 0.0011142006842419505,
        0.0036992731038480997, 0.0029123497661203146, 0.0011071143671870232,
        0.0032149790786206722, 0.00019870192045345902, -0.0005686454242095351,
        0.005012097768485546, 0.00301953312009573, 0.00218176725320518,
        0.0028472288977354765, 0.00028534684679470956, -0.0008731617126613855,
        -0.002721555531024933, -9.343306737719104e-05, -8.250449172919616e-05,
        0.002428097650408745, -0.0014057522639632225, 0.00019750333740375936,
        0.003689037635922432, 0.0038317302241921425, -0.0015036027180030942,
        0.0014431606978178024, -0.004956075455993414, -0.0027192551642656326,
        0.00043002082384191453, -4.082196392118931e-05, 0.0014737430028617382,
        -0.00434843497350812, -0.0008810207364149392, 0.00043270873720757663,
        0.0005371406441554427, 0.00034614658216014504, -0.00025972092407755554,
        0.0047650146298110485, -0.0009968490339815617, -0.002356966957449913,
        -0.0014902063412591815, -0.001119957654736936,
    ),
    (
        0.0027692848816514015, 0.00452546076849103, 0.0010614838683977723,
        0.002304889727383852, 0.004143681842833757, 0.0014532961649820209,
        -0.00016871534171514213, 0.0021996430587023497, 0.00748686958104372,
        0.003078680718317628, 0.006122744642198086, 0.007088279817253351,
        0.05132089555263519, 0.05588889494538307, 0.05816791206598282,
        0.05842182785272598, 0.003406404284760356, -0.003791265422478318,
        0.0016819584416225553, -0.0003801948332693428, -0.005580201279371977,
        -0.004168902989476919, -0.004186726175248623, -0.003993000369518995,
        -0.005205431953072548, -0.005476972553879023, -0.0045888060703873634,
        -0.002421777695417404, 0.0027023053262382746, 0.002057417295873165,
        -0.0032084088306874037, 0.0008731561247259378, -0.03782600909471512,
        -0.027280563488602638, -0.027297867462038994, -0.01044269185513258,
        0.002545560011640191, 0.0006769885076209903, -0.004624918103218079,
        -0.001264780294150114, 0.0008665978675708175, 0.004234627820551395,
        -0.0009841150604188442, 0.004356895573437214, -0.016789274290204048,
        -0.011619621887803078, -0.016309170052409172, -0.012541979551315308,
        0.0011986388126388192, 0.004194409586489201, 0.00017755494627635926,
        8.217765571316704e-05, -0.004402271471917629, -0.0002598506980575621,
        -0.002417230047285557, -0.002586920978501439, 0.0018378455424681306,
        -0.0002395514165982604, 0.002050467301160097, -0.0008707294473424554,
        0.0028382211457937956, 0.005832775961607695, -0.0011329541448503733,
        0.0016617453657090664, 1.630527367524337e-05, -0.002697952091693878,
        0.006262653041630983, 0.003172331489622593, -0.0019500143826007843,
        -0.0032625049352645874, 0.0006693286122754216, -5.892138506169431e-05,
        -0.002339741215109825, -0.00015672558220103383, -0.0016881701303645968,
        -0.008027976378798485, -0.00682775629684329, -0.0041418904438614845,
        -0.001429605996236205, -0.0023731663823127747, -0.0001982292887987569,
        0.0028105515521019697, 0.0022730750497430563, -0.0015523541951552033,
        0.002164091682061553, 0.004002835601568222, -0.002230421407148242,
        -0.002602699212729931, -0.0012716384371742606, -0.0012002679286524653,
        0.0048891883343458176, 0.0012911114608868957, 0.0019010730320587754,
        0.006292331963777542, 0.0001571069296915084, 0.0029545447323471308,
        0.0037529708351939917, 0.0003842232399620116, 0.0005831393063999712,
        -0.001189555274322629, -0.0007639620453119278, 0.0005708681419491768,
        -0.0006069278460927308, 0.001546171260997653, 0.001089361496269703,
        0.0016942339716479182, 0.0019325121538713574, 0.00027734797913581133,
        0.0015092457178980112, 0.0035559318494051695, -0.0020400607027113438,
        0.0015733694890514016, -0.004380906466394663, -0.0026256937999278307,
        -0.0010093118762597442, -0.0017491326434537768, 0.0036383545957505703,
        -0.0031859581358730793, 0.0011547106551006436, 0.0035906629636883736,
        -0.0005358326015993953, 0.0017820722423493862, -0.002990684239193797,
        0.004044759552925825, -0.001744472305290401, -0.0029976358637213707,
        -0.0035976916551589966, -0.002255234634503722,
    ),
)

_FLUX2_BIAS = (0.4698728024959564, 0.4328208565711975, 0.4053916037082672)

#: Stable Diffusion 1.5's fitted map: three rows of FOUR, one weight per latent
#: channel, in the same `rgb = tokens @ factors.T + bias` orientation as klein's
#: above. Small enough to read, unlike the 128-wide table — and the shape of it
#: is legible too: every row's last channel is strongly negative and the first
#: three positive, which is SD's fourth latent channel carrying darkness.
_SD15_FACTORS = (
    (0.16860433707048778, 0.08881676484910431, -0.07116862129767207,
     -0.12419647466837884),
    (0.12687333013700466, 0.12679084904666216, 0.07522314872905711,
     -0.1811777339597075),
    (0.1250038400036917, 0.09710721219961786, 0.10332428963178521,
     -0.22665953899464153),
)

_SD15_BIAS = (0.4768821440901692, 0.43719649267205124, 0.411639848596421)


#: Model key -> everything a preview needs for that model's latent space.
#:
#: * `"factors"` / `"bias"` — the fitted `C -> 3` affine map from packed latent
#:   tokens to sRGB: `rgb = tokens @ factors.T + bias`, then clip to [0, 1].
#:   `factors` is three rows (R, G, B) of `C` weights each, one per latent
#:   channel — `C` is read off `len(factors[0])`, never assumed, so a 4-channel
#:   entry and klein's 128-channel one run through the same `project()`.
#: * `"stride"` — pixels per latent token, per axis, for `token_grid`: the
#:   VAE's spatial downsample times its patchify factor (1 when a VAE is not
#:   patchified).
#: * `"estimate"` — which of the two strategies below `Sink._add` uses to turn
#:   what the callback holds into a frame:
#:     - `"extrapolate"`: recover the velocity between the previous and current
#:       latent and extrapolate it to sigma 0 (see the module docstring for
#:       why a distilled schedule needs this). Step 1 has no predecessor and
#:       gets no frame.
#:     - `"raw"`: project the current latent as it stands. An ordinary,
#:       non-distilled sigma schedule is legible from the first step, so this
#:       strategy needs no predecessor and DOES write a frame on step 1.
#:
#: Keyed by the VAE's CLASS NAME because that is the thing the two engines can
#: both name (see the module docstring), and a TABLE rather than a heuristic
#: for the reason `_GGUF_RECIPES` and `MFLUX_VARIANTS` are: which projection is
#: right for a latent space is a measurement somebody took, not something to
#: infer.
#:
#: A model absent from here is not broken — it renders exactly as it did before
#: this module existed, with no preview file and no branch in its denoising loop.
PROJECTIONS = {
    "AutoencoderKLFlux2": {
        # FLUX.2 klein 4B. Fitted by `fit_factors.py` against the torch VAE's
        # own encode of the repo's three sample jpgs; R² 0.911 / 0.912 / 0.891,
        # residual RMS 0.083 in [0,1]; validated end-to-end on a real GGUF
        # render. See the module docstring for the derivation in full. An 8x
        # VAE with a 2x2 patchify is a stride of 16, and the distilled sigma
        # schedule is why this is the one entry that extrapolates.
        "factors": _FLUX2_FACTORS,
        "bias": _FLUX2_BIAS,
        "stride": 16,
        "estimate": "extrapolate",
    },
    "AutoencoderKL": {
        # Stable Diffusion 1.5, which is `segmind/tiny-sd` here. Fitted the same
        # way klein's was: encode five images (two renders from this app, three
        # of the FLUX repo's sample jpgs) at 512² through tiny-sd's own VAE,
        # scale by the config's `scaling_factor` of 0.18215 so the fit lands in
        # the space the denoising callback holds, box-downsample each source to
        # one pixel per latent token, and least-squares a 4->3 affine over the
        # 20,480 tokens that makes. R² 0.885 / 0.923 / 0.876, residual RMS 0.097
        # in [0,1] — a hair better than klein's, on 4 channels rather than 128.
        #
        # An unpatchified 8x VAE is a stride of 8, and an ordinary SD schedule
        # is legible from the raw latent, so this entry needs neither klein's
        # 16 nor its extrapolation.
        #
        # **`AutoencoderKL` is diffusers' SHARED class name, so this key is
        # broader than the fit behind it.** SDXL's VAE is the same class with a
        # different `scaling_factor` (0.13025) and its own colour statistics, so
        # an SDXL repo loaded here gets a preview drawn with SD1.5's numbers:
        # recognisable, and off in hue. That is the trade the class-name key
        # makes — it is the one string both engines can name — and the fix when
        # a second 4-channel model is worth serving properly is a key with the
        # scaling factor in it, not a heuristic here.
        "factors": _SD15_FACTORS,
        "bias": _SD15_BIAS,
        "stride": 8,
        "estimate": "raw",
    },
}


def project(tokens, model_key: str | None):
    """`(n, C)` latent tokens -> `(n, 3)` RGB in [0, 1], or None.

    `C` is `model_key`'s own channel count, read off its `factors` row rather
    than assumed, so this serves every fitted model's channel width through one
    path. None when nothing has been fitted for `model_key`, which is the same
    answer the sink turns into "no preview": the caller is never handed a
    projection computed with somebody else's matrix.

    **Clipped**, and not as tidiness. The map is affine and unbounded, and an
    early estimate sits well outside the range it was fitted over, so the raw
    numbers routinely leave [0, 1]. A page is being handed a colour.
    """
    entry = PROJECTIONS.get(model_key or "")
    if entry is None:
        return None
    import numpy

    factors = numpy.asarray(entry["factors"], dtype=numpy.float32)
    bias = numpy.asarray(entry["bias"], dtype=numpy.float32)
    rgb = numpy.asarray(tokens, dtype=numpy.float32) @ factors.T + bias
    return numpy.clip(rgb, 0.0, 1.0)


def denoised(previous, current, sigma_previous, sigma_current):
    """The model's guess at the FINISHED image, from two steps of one render.

    `x1_hat = x_next - s_next * (x_next - x_prev) / (s_next - s_prev)` — the
    velocity between two consecutive latents, extrapolated to sigma 0. This is
    the arithmetic behind `"estimate": "extrapolate"`, and the whole reason a
    preview of klein is legible at all; see the module docstring for the sigma
    schedule that makes its raw latent useless.

    None when the two sigmas are equal, which is a division by zero and not a
    frame. Nothing in either scheduler repeats a sigma mid-render, so this is a
    guard rather than a case — but "nothing does that today" is how a NaN
    thumbnail gets shipped.
    """
    gap = float(sigma_current) - float(sigma_previous)
    if gap == 0.0:
        return None
    import numpy

    current = numpy.asarray(current, dtype=numpy.float32)
    velocity = (current - numpy.asarray(previous, dtype=numpy.float32)) / gap
    return current - float(sigma_current) * velocity


def _tokens(latents, grid):
    """Whatever a callback holds -> `((n, C) tokens, (h, w))`, `C` whatever the
    model's own latent channel count is.

    Both shapes both engines produce, in one place, because a runner reshaping
    its own latents first would be a second copy of the unpack rule:

    * `(B, N, C)` — diffusers' packed tokens, and mflux's before its
      unpatchify. Row-major over the token grid, which is `image side /
      stride` per axis for that model's `PROJECTIONS` entry, so the grid has
      to be told rather than inferred.
    * `(B, C, h, w)` — already unpatchified, where the grid IS the shape.

    A grid that cannot be resolved raises rather than silently guessing a
    square: both workers compute it from the width and height they were given,
    so this is unreachable in a render, and a wrong-shaped thumbnail is a bug
    that looks like a picture.
    """
    import numpy

    array = numpy.asarray(latents, dtype=numpy.float32)
    if array.ndim == 4:
        array = array[0]
        height, width = int(array.shape[1]), int(array.shape[2])
        return array.reshape(array.shape[0], height * width).T, (height, width)
    if array.ndim == 3:
        array = array[0]
    if array.ndim != 2:
        raise ValueError(f"latents of shape {numpy.shape(latents)} are not tokens")
    count = int(array.shape[0])
    if grid is None:
        side = math.isqrt(count)
        if side * side != count:
            raise ValueError(
                f"{count} packed tokens are not a square grid — pass grid=(h, w)")
        grid = (side, side)
    height, width = int(grid[0]), int(grid[1])
    if height * width != count:
        raise ValueError(f"grid {height}x{width} does not hold {count} tokens")
    return array, (height, width)


class Sink:
    """A single-frame view of an image being denoised.

    Use it as a context manager around the whole render INCLUDING the final
    save, because the exit is the lifecycle — and here the lifecycle is simpler
    than `partial.Sink`'s in the one way that matters: **every exit discards.**

    A clean exit means the real PNG landed, so the preview is duplicate bytes at
    a thirty-second of the resolution. A cancel means the user pressed ✕ and
    does not want this picture. And an ERROR discards too, which is where this
    diverges from the transcript sink — deliberately, and it is the one
    difference worth stating: a run that died at minute 80 of 90 has 80 minutes
    of words in its partial file and that file is the only salvage there is,
    whereas a render that died at step 12 of 16 has a 32x32 blur of a picture
    that will never exist. Not salvage; just a file in `ai/images/` that no job
    row explains and nothing will ever clean up. That is also why this class
    takes no `cancelled=` argument: with all three exits doing the same thing,
    it never has to tell one exception from another.
    """

    def __init__(self, path: str | None, model_key: str | None = None):
        self.path = path or None
        self.model_key = model_key
        #: Whether a frame will ever be written. Read by the callers, so a
        #: render with no entry in `PROJECTIONS` pays nothing at all.
        self.wanted = bool(self.path and PROJECTIONS.get(model_key or ""))
        self._previous = None
        self._failures = 0

    def add(self, latents, sigma, grid=None) -> None:
        """Offer the step just taken. Writes a frame from the second one on.

        `latents` is a **callable returning** the latents, not the latents, and
        that is the point: reading them off the device is a synchronisation
        that costs most of the measured 68ms, and a sink that is not writing
        must not charge the render for it. Passing a closure instead of an
        array is what lets both denoising loops call this unconditionally —
        the `if preview:` branch `partial.sink`'s no-op exists to avoid.

        `sigma` is the schedule value the run has just arrived AT: after step
        index `i`, diffusers' scheduler has moved from `sigmas[i]` to
        `sigmas[i+1]`, and mflux's `config.scheduler.sigmas` indexes the same
        way off the callback's `t`.

        **NOTHING escapes this method, and that is the whole of it.** It is
        called from inside a denoising callback, which is the one interruption
        point in a `pipe()` / `generate_image()` call that runs for minutes — so
        an exception here does not fail a thumbnail, it unwinds the render and
        destroys work that was going to succeed. The failures are real and are
        not hypothetical: `save` on a full disk, `os.replace` against a Windows
        lock held by the page's own `<img>` reading this very file through
        `/api/fs/raw` (the hazard `discard` already swallows, one line of
        reasoning below), a device that has fallen over inside `latents()`, a
        latent shape nobody predicted. Both runners' `_sigma_after` helpers
        state this invariant; this is where it is actually kept.

        Broad on purpose — a list of expected exception types is a promise
        about a third-party library's failure modes, and getting that list
        wrong costs a render. What a failure DOES cost is the temp file it may
        have left, which is removed here, and eventually the feature: after
        `MAX_FAILURES` in a row the sink turns itself into the same working
        no-op an unfitted model gets, rather than paying a device sync and a
        doomed syscall on every remaining step.
        """
        if not self.wanted:
            return
        try:
            self._add(latents, sigma, grid)
        except Exception:  # noqa: BLE001 - see the docstring; a render is at stake
            self._failures += 1
            # A `save` that landed and a `replace` that did not leaves a temp in
            # a directory the user browses. It is this method's to clean up:
            # `discard` runs at the end of the render, and until then one per
            # failed step would accumulate.
            try:
                os.remove(self._temp_path())
            except OSError:
                pass
            if self._failures >= MAX_FAILURES:
                self.wanted = False
        else:
            self._failures = 0

    def _add(self, latents, sigma, grid) -> None:
        """`add` without the guard — everything that is allowed to raise.

        Which array gets projected depends on `self.model_key`'s `"estimate"`
        strategy (see the `PROJECTIONS` docstring): `"raw"` projects the latent
        this step just produced, `"extrapolate"` recovers `denoised()`'s guess
        from this latent and the one before it. `self.wanted` being true is
        what guarantees an entry exists here — `add` never reaches this method
        otherwise.
        """
        entry = PROJECTIONS[self.model_key or ""]
        tokens, grid = _tokens(latents(), grid)
        sigma = float(sigma)
        previous, self._previous = self._previous, (tokens, sigma)
        if entry["estimate"] == "raw":
            estimate = tokens
        else:
            if previous is None:
                # Step 1: a velocity needs two points. No frame under
                # extrapolation, by design.
                return
            estimate = denoised(previous[0], tokens, previous[1], sigma)
            if estimate is None:
                return
        self._write(project(estimate, self.model_key), grid)

    def _write(self, rgb, grid) -> None:
        """One frame, complete or not at all.

        **Temp file plus `os.replace`, never a write in place.** The page is
        reading this path through `/api/fs/raw` at the same time, and a reader
        that arrives mid-write gets a truncated PNG — the byte-level analogue of
        the half-a-line hazard `partial.Sink.add` flushes around. `os.replace`
        is atomic within a filesystem, hence a temp beside the target rather
        than in `/tmp`. The pid is in its name because a stale temp from a
        killed worker must not be mistaken for this one's.
        """
        import numpy
        from PIL import Image

        height, width = grid
        frame = numpy.asarray(rgb, dtype=numpy.float32).reshape(height, width, 3)
        image = Image.fromarray((frame * 255.0 + 0.5).astype(numpy.uint8), "RGB")
        if max(height, width) > MAX_SIDE:
            scale = MAX_SIDE / float(max(height, width))
            # BOX, i.e. plain averaging: this is a thumbnail of a thumbnail and
            # a sharpening filter on latent-projected noise invents detail.
            resample = getattr(Image, "Resampling", Image).BOX
            image = image.resize((max(1, round(width * scale)),
                                  max(1, round(height * scale))), resample)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp = self._temp_path()
        image.save(temp, format="PNG")
        os.replace(temp, self.path)

    def _temp_path(self) -> str:
        return f"{self.path}.{os.getpid()}.tmp"

    def discard(self) -> None:
        """Remove the preview and any temp beside it. Best-effort by design.

        This runs on the way OUT of the context manager, which on the success
        path is reached only after the real PNG has already been written — so
        anything raised here reports a finished render as a failed one, in
        exchange for a tidier directory. That trade is never worth taking, so
        every `os.remove` failure is swallowed and not just the absent-file one:
        a render cancelled on its first step arrives having written nothing
        (FileNotFoundError), and a page holding this file open through
        `/api/fs/raw` can have a Windows lock on it (PermissionError) at the
        exact moment the image lands. The temp goes too — a frame that failed
        between `save` and `replace` is the one thing here that can outlive its
        writer, and it is in a directory the user browses.
        """
        if self.path is None:
            return
        for path in (self.path, self._temp_path()):
            try:
                os.remove(path)
            except OSError:
                pass

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.discard()
        return False


def sink(path: str | None, model_key: str | None = None) -> Sink:
    """A `Sink` for `path`, or a working no-op one when there is no preview.

    Two ways to get the no-op and they mean different things, but the caller
    treats them identically: no `path` is a request from before this feature
    (and it must run exactly as it did), and no entry in `PROJECTIONS` for
    `model_key` is a model whose latent space nobody has fitted a matrix for.
    Neither is an error and neither is a reason for a denoising loop to grow a
    conditional — that is the argument `partial.sink` makes for its own no-op.
    """
    return Sink(path, model_key)
