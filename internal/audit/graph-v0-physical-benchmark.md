# Graph V0 Physical-PC Benchmark Gate

Date: 2026-09-28. The source prototype and raw JSON/screenshots are retained
locally under ignored `doc/Epic-2/260926_graph_view_replan/bench/`; they are not
published in this repository.

The corrected synthetic scene contained 20,000 visible points and 47,000
relations (35,250 solid, 11,750 dashed). Chrome 153 used an Intel UHD 770 via
ANGLE D3D11, driver 32.0.101.6129, with Node 22.18.0. The corpus SHA-256 was
`8bd82920832cef293c29690b6f173f7e0c77b6d56b3ab7ec672c46cba4f0ec40`;
the layout SHA-256 was
`7a9ac42be6ac8cbed9ea91119fb43cbc052b8d139174a6bf7830f76ca64a7580`.
Initial and final screenshots from the first two controls showed a centered
scene. Coverage measurements reported all 20,000 points projected inside the
viewport for all three controls; simulation ticks and position changes
confirmed continued movement.

Three separate uninterrupted 60-second moving-scene runs yielded frame-interval
p95 values of **143.1, 143.6, and 143.6 ms** (median **143.6 ms**), against a
50 ms gate. They recorded 554, 555, and 532 relation-phase frames; each had
zero browser main-thread long tasks. First visible render and worker Louvain
were below their respective 1.5 s and 2 s limits in the corrected interaction
diagnostics. Three interaction runs separately yielded zoom/pan p95 values of
517.2, 556, and 520 ms, and actual-node hover p95 values of 140, 142.4, and
141.1 ms (33, 33, and 35 hits). These interaction runs are **not** counted as
uninterrupted moving-scene repetitions because their scripted interactions
occupied much of the middle interval.

The first uninterrupted control and the other two had different harness source
hashes, so these three observations are not a qualified same-source repetition;
each independently failed the 50 ms movement limit. The subsequent harness
revision separates a full 60-second moving window from the interaction trials.

In a follow-up, the original 20,000-point/47,000-link physics remained active
while link alpha blending was disabled. A frozen-source three-run measurement
(harness SHA-256 `5346d3441808ae58f95a438f1e88b2c598675ed171d1c52d86f0925e211865f5`)
recorded moving p95 **36.0 ms in every 60-second run**; all 20,000 points moved
in each sampled 20-second interval. First render median was 371.1 ms, worker
Louvain median 718.4 ms, and zoom/pan median 18.1 ms. This is evidence that the
original moving-map scale may be feasible without limiting moving points.
It is **not** a V0 pass: hover p95 was 167, 177.6, and 176.6 ms, with only
30, 27, and 33 hits in 100 attempts. The three runs recorded 4, 7, and 5
post-ready long tasks over 50 ms. The unblended hover screenshot washed out
the whole graph instead of isolating direct relationships, and solid/dashed
line distinction needs a visual proof at representative zoom. This diagnostic
used a muted opaque link color and therefore changes compositing from the
original transparent-link visual.

Targeted hover diagnostics on the same moving scene also missed the 50 ms
gate. A public synchronous rectangle pick plus a one-hop overlay took 122.5 ms
at p95 from pointer to paint and produced 41 long tasks; a 6 Hz CPU position
grid took 89.2 ms and 32 long tasks; forcing the library's private synchronous
point pick took 99.0 ms and 33 long tasks. These were short diagnostic runs, not
gate repetitions. The one-hop overlay could display a star without adding
whole-scene greyout, but position readback/picking stalls are not acceptable.
The library's default hover path intentionally delays detection by four frames,
which matches most of the measured callback delay at the faster moving cadence.

Further ignored diagnostics narrowed the visual/hover problem but did not pass
V0. A pinned shader patch removed the unblended grey washout; its initial
solid/dashed links and hover star were too faint. A cursor-local GPU picker
initially selected the right point but drew no visible overlay. Submitting its
winner pass with `gl.flush()` made the gold center and incident rays visible on
the Intel GPU in a short smoke, without hover-path CPU readback. That first
overlay still had uniform solid rays and no distinct neighbor points.

An integrated pinned-shader/GPU-hover prototype then drew a gold center,
colored direct-neighbor points, grey solid rays, and teal 7/4-pixel dashed rays
in an actively simulating 20,000-point/47,000-link scene. The physical-PC
short smoke recorded **71.8 ms** moving-frame p95 with the pointer on canvas
(1.49 seconds, 24 frames, 90 simulation ticks). This interval lies entirely
inside the gate's first-five-second warmup, so it cannot by itself decide the
post-warmup 50 ms criterion. Removing
the neighbor-point draw alone left p95 at 71.7 ms. The unhovered base links
were nearly invisible; source inspection found that the prototype set new
link colors without a subsequent `graph.render()` to upload them. Neither the
visual fix nor the full three-by-60-second interaction/memory gate has been
verified. The ignored result records active-source SHA-256
`3b134031ccf64f0caf37fdd0338f1d30e63794b22bb61ed1267a2d43b37798e7`
and served-bundle SHA-256
`a19b186b49974b7aa66b041918b66cd6971b0d83af450afb30f9786aadc85dcd`.
The original first-render timer ends before this integrated overlay is ready,
so it is not a valid integrated-first-render measure. A separate WebGPU
renderer candidate submitted the same 20,000 nodes and 47,000 links on the
Intel adapter and had 36 ms rAF p95 over 20 seconds, but physics updated only
227 times (~11 Hz; tick p95 165.1 ms), with many rendered frames reusing old
positions. It also lacks the required line styles and hover proof, so it is
only a partial performance floor, not a substitute V0 result.

A later ignored, degree-bounded CSR variant uploaded base-link colors, and
replaced the fullscreen hover wash with a 142-by-142 GPU point-status mask and
native shader link dimming. Its current-position GPU winner drives at most 150
incident line vertices and 75 neighbor vertices per frame; the base scene
still moves all 20,000 points and 47,000 relations. On the Intel PC, a 12-second
uninstrumented 1 px base-link trial recorded **54.0 ms** moving-frame p95 after
five seconds (171 frames), 289 simulation ticks and zero long tasks; the
0.5 px variant recorded **54.1 ms** (177 frames). A bounded variant removing
only the second status-mask `gl.flush()` preserved the one-shot visual/status
checks but yielded **54.1 ms** post-warmup p95 (169 frames, 288 ticks), not an
improvement. Its active-source SHA-256 was
`3e86dbb8334a174b108c336d40c8a134fb205cd2a0fcdcb5fb654b0681fef174`
and served-bundle SHA-256 was
`2d2d7cbcfeef5a18a805fd8d9993d899849a87b721befa52d0e9d304148743cc`.
The unmodified 1 px trial source/bundle hashes were
`74c189773df986d25d24207f12940f37fddfa03da98578b27ae67d5cf2500796`
and `2aebefc3f9fd2e30988d843148181fd13998ff61fbc5d46628f8f4e50b39e3e3`.

One-shot readbacks verified the gold selected point, selected/neighbor status
undimmed, unrelated status dimmed, styled direct rays, and normal restoration
after pointer exit or an invalid winner. A separate 500 ms-sampled run saw a
valid gold winner in all 25 samples, but its synchronous readbacks raised the
post-warmup p95 to 125.1 ms and produced 24 long tasks; that run is **not**
performance evidence. The uninstrumented runs did not measure the fraction of
frames with a valid visible star, and the fixed cursor's winner changed as the
scene moved. The 1 px base-link mesh is more visible, but individual solid and
dashed styles remain difficult to distinguish at the tested overview zoom.
Neither 12-second trial is a three-by-60-second repetition, a true
pointer-to-visible-star latency distribution, or a GPU-memory measurement.
The integrated first-paint candidate was about 1.3 s in the no-status-flush
trial, but is not a qualified first-render gate measurement.

Further 12-second Intel-PC A/B trials isolated the point-status mask as the
remaining hover-path bottleneck: deliberately disabling that mask reduced the
post-five-second moving-frame p95 to **36.1 ms**, while disabling link dimming
instead yielded **54.1 ms**. The no-mask image leaves unrelated points bright
and is visually invalid. A one-channel R8 mask with a GPU conditional fill
produced **50.1 ms** p95 (167 post-warmup frames); a separate R8 framebuffer
clear plus at most 76 CSR marks also produced **50.1 ms** (161 frames). Both
retain 20,000 moving points, 47,000 links, a gold selected center, undimmed
selected/direct-neighbor status and dimmed unrelated status in one-shot
checks. The fast-clear run recorded zero JavaScript/network/GPU errors and
zero main-thread long tasks over 50 ms, and exercised forced pointerdown
through stock-highlight fallback and pointer-leave restoration after timing.
Its integrated first-paint candidate was 1,392.5 ms, not a qualified repeat.
R8 source/bundle hashes were
`e08e9b2b3c2704d77e3ad170835c80c85a11c630948b43c3f4297963f5c139d5` /
`5582e6de4ff76f9508b8fac1686125a9d0fff61e3629ec1df8672da45b1b6b65`;
fast-clear source/bundle hashes were
`32e167fa44633c2c2171394fc48f351e76e7aaefc77f1f26bc8232d0b49ee47a` /
`44fff218022c2d619a1182a3c6a76e5173d07aeb121e9cd239afe8ea4c354bb6`.
The raw hash-suffixed JSON and screenshots remain in the ignored benchmark
directory. Neither R8 trial meets the strict 50 ms limit or substitutes for
three independent 60-second runs and sustained visual-star proof.

A separate, instrumented 60.25-second physical-PC run on the baseline
RGBA32F variant sampled the GPU winner and framebuffer center in the same
post-overlay frame with asynchronous PBO/fence readback. All **240/240**
scheduled samples had a valid gold center (20/20 in each five-second bucket),
with no dropped samples or observed JavaScript/network/WebGL errors. Source
and served-bundle hashes stayed fixed at
`f17fab915a11718245682cff361013552fa98f939fe762372e59a00dfb057844` and
`8c54172262b3482abe95ed63215aabc27428f487a0fa5ec354969c46d4a78903`.
An earlier 240/240 run was excluded because the probe script changed during
the measurement. The valid repeat proves only sampled center persistence for
that baseline variant: the probe changes frame timing, and did not sample
status-mask pixels, incident rays, every frame, or any R8 candidate. It is
not a moving-performance or full visual-star gate result.

A third provisional shader variant eliminated the point-status framebuffer
entirely: the pinned native point shader globally dims points while a valid
GPU winner exists, and the bounded overlay redraws the direct star. Its
12-second physical-PC run still recorded **50.1 ms** post-warmup moving p95
(160 frames), with 20,000 moving points, 47,000 links, a gold center and no
observed JavaScript/network errors or long tasks. Its source/bundle hashes
were `9152298837054ea6ccc64f03a7149118c3f3c3137bccce5472d3b29501f14b27` /
`77cf1c78926d3bb95f17ddc5d3e0a8dff31578bb1385ca23fe9a6021e6e0ebf2`.
The smoke's global-dim counter and gold center do not prove that unrelated
native pixels were correctly dimmed: the gold center comes from the separate
overlay. Its screenshot looks similar at overview zoom but pixel-level
contrast/style parity, higher zoom, and production behavior remain unverified.
The variant therefore also fails the strict 50 ms criterion and is not a V0
pass.

Further same-hub, zoomed 12-second diagnostics tested the native point draw.
After five seconds, a repeated global-dim control had moving-frame p95
**50.1 ms** (161 frames, 275 simulation ticks). Disabling Cosmos point
occlusion culling made no improvement: **50.1 ms** (165 frames, 283 ticks).
Mixing greyed point RGB with the known background while keeping an opaque
core gave **49.9 ms** once (204 frames, 352 ticks), but combining that mix
with no occlusion returned **50.1 ms** (169 frames, 291 ticks). All four
selected the same 75-relation mixed-style hub, showed a gold center, and
recorded no JavaScript/network errors or >50 ms long tasks. The control,
no-occlusion, and combined trials shared active-source/bundle hashes
`d1aacc3ba7cdac590898188e23f630e663c19d69ddf06076417b7fe98ea80640` /
`291b59be346bf199ff2bcec7b8e45e0c285a42bb2275f6a305dd6dd3b26c84a8`;
the earlier opaque-only trial used
`d7cb9bcc8549369b4b31fb5e0b78eaeebf30dcb15700d283bfb7836549423582` /
`eede721413f50679a946da82543b39bcc4600a5ddb5e17c7c4f37e8f4a645aa9`.
Opaque precomposition changes overlap/link compositing, and no-occlusion
pixel parity is unproven. One narrow 49.9 ms short smoke is not a gate pass.

A source-frozen, four-leg no-point-mask / global-dim / global-dim /
no-point-mask cadence diagnostic then used the same detail zoom and began
each leg on that 75-relation hub. After-five-second rAF p95 values were
**49.8 / 50.1 / 50.1 / 49.9 ms**; full-frame CPU p95 values were
**5.6 / 6.1 / 4.6 / 6.0 ms**. The idle display cadence was 16.8 ms in every
leg. The visibly invalid no-mask legs had median active intervals of 33.3 ms,
versus 49.9 ms in both global-dim legs. The fixed cursor's GPU winner changed
as the graph moved, so these are renderer-attribution diagnostics, not exact
same-star or GPU-timer proof. The raw report is
`shader-dim-probe/results/cadence-abba-2026-09-28T03-59-13-804Z-c20d79d979cf.json`.

An attempted three-by-60-second physical full-map gate on the baseline
RGBA32F/1 px configuration produced one complete run and two pre-window
failures. At fit zoom, the projected 75-relation target was occluded by a
different valid picked point in runs 1 and 2, so neither started a moving
window. Run 3 recorded **33.5 ms** post-five-second moving p95 across 1,843
frames, integrated first-paint candidate **995 ms**, worker Louvain **544.6 ms**,
island calculation **59.4 ms**, and zero >50 ms moving-window long tasks.
Zoom p95 was **50 ms** (limit 33), pan p95 **33.4 ms** (limit 33), and only
23/30 intended hover targets matched the picked node; successful hits had
7.1 ms p95 pointer-to-paint candidate latency. All seven misses picked a
different valid, gold-centered node, exposing dense-scene target-selection
ambiguity rather than proving the hover renderer wrong. The WebGL allocation
estimate was 54.66 MiB with eight unknown allocations; no per-run browser
Task Manager GPU-memory reading was taken. The moving window had no in-window
star sample, and hash-bound visual/style proof remains absent. This run's
source/bundle hashes were
`e845b1cce39294a9149f3c00d500f5fdb9c25072da2fc94665e2084b6743e56a` /
`945dff8181cde16c8580284529ca7d1d793957a9053983e238f8b2786d11af38`;
raw report:
`shader-dim-probe/results/full-gate-2026-09-28T04-06-10-158Z.json`.
The whole attempt failed and cannot be treated as a three-run pass. It does
show that the full, unhidden 20,000-point map can meet the moving sub-gate
on this PC in one uninterrupted run; zoom/pan and reliable target acquisition
are the immediate measured gaps.

An opt-in star-only vertex-cull build was then tested on the same Intel PC.
When the GPU winner is valid, native visible point and link vertices are
clipped before raster work; the bounded CSR overlay draws only the picked
point's direct relations. Its picker, all 20,000 physics points, all 47,000
simulated relations, and the unhovered native draw remain present. In a
12-second zoomed, 75-relation-hub short smoke, post-five-second moving p95
was **33.5 ms** (206 frames, 417 simulation ticks in the whole window);
actual wheel-event zoom p95 was **16.8 ms** (96 frames), and drag-event pan
p95 **16.7 ms** (19 frames). The same-source global-dim control measured
**50.1 / 49.9 / 16.8 ms** respectively (164 post-warmup frames). Both had a
gold selected center and no observed JavaScript/network errors or >50 ms
long tasks. Source hash was
`c691d5d1dc2dd5f23753b7dbdfb99f9af23145750738c7257a010a96bc5605b6`;
star-only and control served-bundle hashes were
`074b750c317b199c08a781980306cf26c5e8f90820542bc1c08e37902681d3fc` /
`61b20a1b2de135013134a23fcc8a7088052cc7e0ec0ec72a0088c3bbaa51a395`.
At detail zoom, a one-shot 4-pixel-stride framebuffer sample found 70.3% of
pixels non-background without hover, 3.4% with the direct star, and 69.9%
after pointer exit or at an invalid inside-canvas winner. Styled grey solid
and teal dashed direct rays, colored neighbor shapes and a gold center were
visible in the active screenshot; the full map returned on exit. These are
one-shot observations, not frame-by-frame proof. The fixed cursor's winner
was invalid at the end of the 12-second moving interval, so continuous
visible-star persistence remains explicitly unproven. Further, a cheaper
hover scene must **not** substitute for a separately timed, fully visible
20,000-point/47,000-relation moving map. Three 60-second runs, in-window
winner/center sampling, true zoom/pan and hover gate repetitions, hash-bound
visual review, and browser Task Manager GPU memory are still required.
Raw results are the hash-suffixed `width1-12s-star-only-zoom-pan-*` and
`width1-12s-global-point-dim-zoom-pan-*` JSON/screenshots in the ignored
benchmark directory. Star-only is a provisional visual policy; it is not a
V0-T3 pass or production integration.

A separate 60-second **diagnostic-only** star-only run on Chrome 153 / Intel
UHD 770 (D3D11, driver 32.0.101.6129, DPR 1) checked the GPU winner and
gold center in the same post-overlay frame at 4 Hz. All **240/240** scheduled
samples were valid, including **20/20** in each of twelve 5-second buckets;
the window lasted 60.05 seconds, and simulation ticks advanced from 111 to
3,402. The runner sent no pointer movement during the window and sampled
cursor coordinates matched the initial target, but it did not inspect every
intermediate pointer event. The GPU winner changed across **11 IDs**; the initial
mixed-style, 70-relation hub was selected in only **55/240** samples. This
supports a visible center for the *current* picked point at sampled instants,
not 60-second persistence of one hub or correctness of every ray/style/frame.
The sampler allocated 6,302,400 bytes of PBO storage and issued GPU readback
commands inside the window, so this run provides **no performance verdict**.
Its source/built/served manifest hashes were
`e2093457c3338bb7af1af38cca14a22ed3a9f5bf3b6638de75048d6e20b9fc1e` /
`074b750c317b199c08a781980306cf26c5e8f90820542bc1c08e37902681d3fc` /
`074b750c317b199c08a781980306cf26c5e8f90820542bc1c08e37902681d3fc`.
The ignored runner and sampler file hashes were
`b6ca77055febe2bd7116a644e9ee6474aba76ebc15538a93a864cfac45234e1e` /
`8499024a94282ce9012e2737ddbfee5de005883169e94de756d4b900c5bef547`.
Raw report: `shader-dim-probe/results/in-window-star-smoke-2026-09-28T04-34-55-346Z-5ae6036c71c4.json`.
Its independently checked file SHA-256 was
`e12f9b2f73aa3ce23139371823b595d591782d80687995d573939098c8dde327`.
An earlier attempt stopped before the timed window because a negative
served-bundle string check mistook dormant bundled status-mask model names
for an active GPU path. The corrected runner keeps the fail-closed runtime
checks that no status mask was allocated. Neither attempt qualifies V0-T3;
the separately timed full visible map and the complete 3-by-60-second gate
remain necessary.

The first opt-in star-only **three-run full-gate attempt** then recorded a
separate unhovered full-map and hovered-star 60-second moving window in each
run. Independently checked window timestamps and traces were ordered and
complete. All three unhovered windows measured **33.4 ms** post-five-second
rAF p95 (2,364 / 2,360 / 2,356 frames), and all three hovered windows
measured **33.3 ms** (3,020 / 3,022 / 3,018 frames). Actual wheel zoom and
drag pan p95 were **16.8–16.9 ms**; worker Louvain was **546–555 ms**;
the first integrated paint *candidate's page-clock timestamp* was
**1,001–1,096 ms** (renderer-start-relative **912–1,002 ms**). No >50 ms
moving-window long tasks or JavaScript/network/GPU errors were recorded.
Each hovered window captured 240/240 valid gold-center samples, but the GPU
winner changed across **9 / 9 / 23 IDs** respectively; the initially verified
hub was selected in only **33 / 34 / 7** of those samples. This does not prove
one hub's full star over all frames. Intended-target hover matches were
**28/30, 28/30, 30/30**: the first two runs missed the harness's 95% target
reliability threshold, although the misses selected other valid visible
points. The reported 4.7–6.3 ms hover-paint p95 is **not qualified**: review
found its timestamp need not wait for a new graph overlay after the measured
pointer event. The WebGL resource estimate was **54.80 MiB** with eight
unclassified allocations, and no per-run Chrome Task Manager GPU reading or
hash-bound style review was attached. The gate result was **unqualified**, not
passed, despite favorable reported timing numbers.
Runner/source/bundle SHA-256 were
`89c5f821b60aba32e4c29bdfe0214455e71ceeb73211c9fb86e7c0f950624736` /
`d2f563fe885767cf48e6e018f025bae2852c9934d1957103e4dc351f8e1eff2c` /
`074b750c317b199c08a781980306cf26c5e8f90820542bc1c08e37902681d3fc`.
Raw report: `shader-dim-probe/results/full-gate-2026-09-28T04-39-46-470Z.json`
(file SHA-256
`80d2db8211db8db5bda7ec04910f154475e43c9ea40feb8499495c9a7c57b246`).
The needed meter and hover-harness changes require a fresh source-frozen run;
they cannot retroactively qualify this record.

The WebGL resource estimate peaked near 52.23 MiB but excludes browser/GPU
process overhead. A separate one-time Windows GPU Process Memory counter sample
for this Chrome process tree read 200.58 MiB shared usage. Chrome Task Manager
peak GPU memory was not measured, so the memory sub-gate remains unqualified.

Early benchmark runs are excluded: their initial screenshots were blank or
cropped. The harness had fitted the camera before the first render and had fed
negative coordinates to a simulation space that clamps at zero. The corrected
scene translated coordinates into positive space, rendered before fitting, and
checked viewport coverage and screenshots before timing. The final runner was
subsequently adjusted to keep moving and interaction windows separate; the
frozen-source unblended follow-up used this revised measurement sequence.

Node 22.18 local desktop checks passed: 26 desktop tests, frontend typecheck
and Vite build, Linux x86_64 PyInstaller engine build, and an isolated
temporary-data engine smoke covering MCP client inventory, `/health/ready`, and
clean shutdown. The build script started with no existing `desktop/build`
outputs, and no tracked files or `backend/static` were changed. The frozen
engine carried the pre-existing static bundle rather than the freshly built
frontend, and Windows/macOS installers plus full CI were not exercised; V0-T1
is therefore not claimed as fully qualified.

**Decision at the earlier checkpoint:** the stock-blended V0 failed the
sustained-movement gate on this physical PC. The unblended follow-up passed
only the continuous-motion sub-gate; the newer integrated visual/hover short
smokes also exceeded the post-warmup frame limit, even after R8 fast-clear
reduced it to 50.1 ms p95.
Hover, visual fidelity, long-task consistency, and the browser GPU-memory
sub-gate remain unqualified.
F14′ and V2 remain closed pending an interaction/visual correction and another
complete V0 gate. No static or blank scene is accepted as a substitute for the
moving 20,000-point map.

## Subsequent source-frozen three-run V0 attempt (2026-09-28)

A corrected star-only runner on the same Chrome 153 / Intel UHD 770 D3D11 PC
completed **three** separate repetitions. Each repetition timed an unhovered,
fully visible 20,000-point/47,000-relation moving map for at least 60 seconds,
then a separately timed valid-hover direct star for at least 60 seconds. The
unhovered map's post-five-second rAF p95 was **33.4 / 33.4 / 33.4 ms**; the
star's was **33.3 / 33.3 / 33.3 ms**. First integrated paint on the page clock
was **1,424.3 / 989.0 / 982.1 ms**; worker Louvain took **550.1 / 555.5 /
547.4 ms**. Actual wheel zoom p95 was **16.8 ms** in all runs, drag pan p95
**16.8 / 16.8 / 16.9 ms**, and both moving windows had zero observed main-thread
long tasks over 50 ms. The in-window asynchronous sampler found a valid GPU
winner and gold center in **240/240** slots per run; it selected **23 / 23 /
22** different winners as the scene moved, not one persistent hub. All WebGL
allocations were classified, with an estimated peak of **54.80 MiB**; this is
not browser GPU-process memory.

Thirty fresh hover trials per run had a first post-pointer overlay paint and
the same actual GPU winner in two consecutive draws: **30/30** valid hits per
run, with paint p95 **5.0 / 4.4 / 6.9 ms**. Confirmation through the second
GPU-winner readback had a separate p95 of **82.7 / 90.7 / 74.3 ms** and must
not be described as a sub-50-ms confirmed hover. The winner matched the
harness's prechosen target **29/30, 27/30, and 28/30** times; the other trials
picked another valid point within about 2.2 CSS pixels of the pointer. Exact
target acquisition therefore remains a distinct usability question. No
JavaScript, failed-request, HTTP, or WebGL errors were observed.

All **21** PNG screenshots (seven states per run) match both the raw report
and visual-proof template SHA-256 hashes. Independent review confirms the
captured hover state's gold center, direct solid grey and dashed teal rays,
circle and square neighbours, and return to the full map on pointer exit or
invalid pick. However, in the **unhovered** overview and detail captures the
base links are too faint to distinguish the teal dashed links or verify their
7/4-pixel pattern. A still image also cannot count all rendered points and
links, nor prove the style persists through every moving frame. The template's
first visual check is therefore **unproven**, and no visual-proof approval was
attached.

Source inspection found 11,750 citation-style links and a committed 47,000-link
color buffer, so this is not evidence of a missing style upload. The prototype
uses 0.5-pixel base links at DPR 1 and Cosmos's default distance-fade floor
of 25%; the design calls for a 3% minimum. That floor is already brighter
than the design, so increasing alpha or suppressing fade would not silently
solve the proof gap. The served bundle includes a pinned shader patch that
discards zero-alpha dash gaps and precomposes link opacity against the opaque
background in the unblended pass. Thus reading only the upstream Cosmos shader
would incorrectly imply that native dashes and fade are wholly absent;
overlapping unblended links still overwrite rather than source-over composite.

A separate, **pause-only** native-base visibility diagnostic used the same
frozen source and served bundle. At actual zoom 3.136, a 306.6-pixel citation
segment was fully on screen with the pointer off canvas; runtime counts were
20,000 points, 47,000 links, 35,250 solid and 11,750 dashed styles, with the
style and color buffers present. The screenshot still did **not** make the teal
7/4 pattern reliably distinguishable by eye. A narrow pixel sample showed
teal excess along the segment relative to a parallel control and periodic
light/dark bins, but that is not user-visible style approval. The diagnostic
paused simulation after integration and is **not** moving-gate evidence.
Its ignored report is
`shader-dim-probe/results/native-base-visibility-2026-09-28T05-57-41-937Z.json`
(SHA-256 `d4eb427bc520e12e544bcc130584a1e15daa220f17c36618e0490f1eac490610`);
the zoom screenshot SHA-256 is
`159e39b138cad2a60e67d62b75c3af2a8c356121a1a7ebd246338202355c02a6`.
A 1-pixel base-width variant would need explicit design review and a new full
measurement, not a retrospective approval of the 0.5-pixel images.

Chrome Task Manager GPU-memory evidence was **not** collected across these
UTC measurement windows; a later sample cannot qualify them retroactively.
The raw evaluator correctly returned `unqualified`, with no measured limit
failures and exactly two missing proofs: hash-bound per-run browser GPU memory
and hash-bound visual/style approval. The physical timings are encouraging,
but V0-T3 has **not passed**; F14′ and V2 remain closed. A further frozen-source
run needs a defensible base-link visibility proof and simultaneous browser GPU
measurement before making a pass claim.

Raw report (local, ignored benchmark directory):
`shader-dim-probe/results/full-gate-2026-09-28T05-30-10-517Z.json`.
Its SHA-256 is
`364ff66274176b12a6d67b50865403067da1048ec11a28ac25e730443e72c67f`;
the source manifest is
`468d98bd63097ef7bff583bf748d256ba950595202ea6fb2e3e0d00ca447ca34`
and both built and served bundle manifests are
`1367c96987d3c3328cf3c675e2faf95c3d849bf47470eaa4cd3677ca2e245dd5`.
The report records benchmark commit `e37086225793cd384b3529267815958735eb2bf2`.

## Native base-link width comparison (2026-09-28)

An opt-in 1-pixel base-link run retained the same synthetic 20,000 points,
47,000 links (35,250 solid / 11,750 citation-dashed), color palette and 7/4
dash pattern. On the Intel UHD 770, a distinct unhovered moving-map diagnostic
lasted **12.41 seconds**, advanced **511** simulation ticks, and measured
**33.4 ms** rAF p95 over **510** frames with zero >50 ms main-thread long
tasks and no observed JavaScript, request or WebGL errors. A separate 12.22 s
hovered-star short smoke measured **49.9 ms** post-five-second p95 over 211
frames. These short, diagnostic-only windows are not three uninterrupted
60-second repetitions or a GPU-memory gate.

After the moving interval, simulation was paused solely for a same-camera
visual comparison between 1.0 px and 0.5 px. The selected native citation
segment was **38.7 screen pixels** long, inside the nominal 10–60 px unfaded
range, with no other point within 8 px of its midpoint. The 1-pixel view makes
the grey mesh stronger, but independent inspection of the full and clipped
screenshots still cannot reliably distinguish its teal 7/4 dash. Neither width
qualifies the unhovered style check. This A/B kept Cosmos's 25% fade floor and
fixed 10–60 px range, whereas the design requires a **3%** floor and a range
that expands with zoom. A design-conformant fade experiment must be assessed
separately; simply selecting the thicker width is not a visual or performance
gate pass.

Ignored comparison report:
`shader-dim-probe/results/width-one-visibility-2026-09-28T06-09-44-477Z.json`
(SHA-256 `b7e9254e7cadd30e482f474280f82cd5d911a49fdb98f53c1e1a9a7e4f45dfc6`).
Its source hash is
`7e9ca15a28d2c9dde8338c5df8fd4c7ff78387e2b71d148a4cb24e441bdd7763`,
served bundle hash
`1367c96987d3c3328cf3c675e2faf95c3d849bf47470eaa4cd3677ca2e245dd5`,
and the 1-pixel detail-clip SHA-256 is
`7317e647e4e0dfee433758880fabdf4bb0e44e9fa1892b063b97d13571cceb73`.

## Design-fade comparison (2026-09-28)

An opt-in 0.5-pixel variant set the plan's **3%** long-link fade floor and
expanded the **10–60 px** fade range linearly with camera zoom through the
public Cosmos `onZoom`/`setConfigPartial` API. A pure pan left the fade
configuration unchanged. With 20,000 points and 47,000 links still moving,
a **12.31-second** unhovered diagnostic yielded **33.4 ms** frame-interval
p95 over **532** frames and **534** simulation ticks; no >50 ms main-thread
long tasks, JavaScript/network errors, or GPU errors were observed. This is
not a three-by-60-second gate or a Chrome Task Manager GPU-memory result.

At camera zoom **0.392 → 1.273**, the range changed **[10, 60] → [32.49,
194.94] px**. One citation grew **37.5 → 121.8 px**, so its normalized fade
factor remained approximately constant: linear range scaling alone does not
make the same link clearer on zoom. Paused, same-position screenshots compared
the design setting with the old **[10, 60] px / 25%** setting; independent
inspection still could not reliably distinguish native teal 7/4 dashes from
the grey base mesh. The source's explicit 3% floor fixes one documented
divergence, but neither the screenshots nor the short timing run qualify the
visual or full V0-T3 gate. Any stronger zoom-reveal policy would be a separate
design choice requiring its own A/B and zoom/pan performance check.

Ignored report:
`shader-dim-probe/results/design-fade-visibility-2026-09-28T06-21-40-582Z.json`
(SHA-256 `acb6d88468aa953db46d00fa4163673ed97c8fb5a4af4b72d57ae89dff44bada`),
source hash
`baa5f2fc4d793d3d66cba76e4363775fa2690c9ddd3c02938d68a505fedad371`,
built/served bundle hash
`9778c8822e28d0260ca2ad532d39791b075dc9ee39c15cd8555c4a92ea76616e`.

## Citation render-order comparison (2026-09-28)

A diagnostic stable-partitioned the 47,000 links passed to Cosmos, drawing
35,250 solid links before 11,750 dashed citation links. The source corpus,
worker layout input, all endpoint/style triples, and order within each kind
were preserved; the Node 22 buffer test passed. Cosmos also consumes this
order in its link-force inputs, so an equal paused position hash does not prove
identical future simulation trajectories. The baseline and reordered
screenshots used the same nonempty position hash, camera and 39.088-pixel
native citation segment. Drawing citations last raised summed local teal
contrast from 47 to 168, but **zero of 118** nearby pixels in either image
reached a +15 green-minus-red contrast threshold. Independent visual
inspection found no plainly legible 7/4 dash. The ordering change alone is
therefore insufficient to qualify the unhovered map style.

Separate 12-second live-map diagnostics measured **33.5 ms** baseline and
**33.4 ms** reordered frame-interval p95, with 403 and 402 simulation ticks
and no observed WebGL or JavaScript errors. These are diagnostic windows, not
the required three 60-second runs or browser GPU-memory evidence. The order
variant remains opt-in and unadopted. Ignored report:
`shader-dim-probe/results/citation-order-visibility-2026-09-28T06-34-23-954Z.json`
(SHA-256 `88548cdc3380bd465b5f693428647524296e5ded22d8afb928bf592941fd1b36`).

## Citation-specific width comparison (2026-09-28)

An opt-in native-width experiment kept the 35,250 grey solid links at 0.5 px
and used Cosmos's public width buffer to draw only the 11,750 teal citation
links at 1.5 px. The edge order, endpoints, kind, color, alpha 0.35/0.5, and
7/4 dash remained unchanged; the 47,000-width getter and GPU buffer were
verified. All variants used the plan's 3% long-link fade floor. A third,
comparative variant also rendered citations last, but that reorders Cosmos
link-force inputs and is not a purely visual change.

With the same 20,000-point positions and camera, a 39.088-pixel citation had
summed local teal contrast **34 / 298 / 791** for baseline / citation width /
width plus last-rendered order. Pixels above a +15 threshold numbered **0 /
8 / 23** out of 118. At a paused 2.94x detail zoom, the same segment measured
117.265 pixels and contrast was **469 / 2,516 / 3,944**; pixels above +30 were
**0 / 35 / 63** out of 354. Teal texture increased, but inspection of the
actual screenshots still could not reliably distinguish the target's 7/4 dash
through crossing links. Zoom-scaled fade ranges grew **[25, 150] → [75, 450]
px**, leaving the normalized fade unchanged rather than making that link
clearer. Neither setting qualifies the visual criterion.

Separate 12-second moving-map diagnostics measured **33.4 / 33.5 / 33.5 ms**
frame-interval p95, with no WebGL or adapter errors; these are not the
required three 60-second repetitions. The width-only variant avoids the
force-order side effect but is not adopted. Ignored reports:
`shader-dim-probe/results/citation-width-visibility-2026-09-28T06-42-32-631Z.json`
(SHA-256 `213f9936fc88077eaa5ec74498c5b783f5dfd408eada1284db93a010d7e89e8c`)
and `shader-dim-probe/results/citation-width-detail-2026-09-28T06-45-43-131Z.json`
(SHA-256 `58d4e25c045b5fa76f1d92e4fac5fbf19e230abe5d773afdf3f7ac1b8c5fe9d8`).

## Citation crossing-density check (2026-09-28)

To avoid judging a hand-picked isolated line, a paused same-geometry probe
ranked **657** visible 30–60 px citation segments by nearby line encounters.
The lower-decile example (35.59 px) still had **61** geometric crossings and
85 near-line encounters; the median example (43.39 px) had **100** crossings
and 122 encounters. The 16 px spatial grid and 2 px near-line rule approximate
clutter rather than measuring exact pixel overdraw. Baseline and 1.5 px
citation-width captures shared position, camera, link/style hashes, 7/4 dash,
palette and design fade. Neither the lower-decile nor median target showed a
dependable 7/4 rhythm at native screenshot size. Clutter is a plausible
contributor, not proven to be the only cause; insufficient alpha and
antialiasing may also matter.

The Intel Chrome WebGL2 default framebuffer reported depth enabled with
**24 depth bits**, making a visible-only depth-priority diagnostic technically
testable; it does not establish its visual or performance outcome. This was a
paused visual probe, not a moving V0 measurement. Ignored report:
`shader-dim-probe/results/crossing-density-visibility-2026-09-28T06-54-52-409Z.json`
(SHA-256 `c14aef9e612071fc2ce38bffc11869505ed730032c931d9fc50a0fc1ac6bc573`).

## Rejected depth-priority link diagnostic (2026-09-28)

A separate, opt-in visible-link shader variant used the browser's confirmed
24-bit default depth buffer to place dashed citation fragments ahead of grey
solid fragments without changing the original 47,000-link order or physics
input. It left the link-picking pipeline separate. Same-camera captures and
runtime checks retained 20,000 points, link/style/position hashes, point-center
pixels, hover, forced pick, and pointer-exit restoration; WebGL errors were
zero. Even so, independent inspection could not read a reliable 7/4 citation
dash in either the depth-only or depth-plus-1.5-pixel captures.

More decisively, separate **12.39-second** live-map windows measured frame
interval p95 **33.5 ms** for the base, **50.1 ms** for depth-only, and
**50.1 ms** for depth plus citation width. Both experimental values exceed
the ≤50 ms moving-frame gate before a full repetition. The depth variant was
rejected and its optional source edits were reverted; the default pinned
shader patch hash/verification returned to the prior state. These short runs
are not the three 60-second V0 gate or browser GPU-memory evidence. Ignored
report and screenshot manifest:
`shader-dim-probe/results/depth-priority-visibility-2026-09-28T07-02-36-501Z.json`
(SHA-256 `179db42f3720e0eaaae695fe7ea4b27e0c6043d71ba17f17d3dceeaf112c4f20`).

## Opt-in citation foreground diagnostic (2026-09-28)

An ignored, default-off `citationForeground=1` variant leaves the original
20,000 points, 47,000 native links, physics order, and picking IDs in place.
It uploads the 11,750 citation endpoint pairs once and uses current GPU point
positions to draw a citation copy between native links and native points in
the existing render pass. The source color is teal with a 7/4-pixel dash and
alpha 0.35; it is **not** a style-conformant final compositor because the
native citation remains underneath and source-over compositing can raise the
effective pixel opacity. The added 70,500 vertices may also change frame
cadence, which was not measured on the user's physical GPU.

Node 22.18 focused tests passed 7/7, and an isolated star-only Vite build and
pinned Cosmos verification passed. A headless software-renderer smoke found
zero foreground draws with the flag off and six after three seconds with the
flag on; it observed the gold direct-star hover, pointer-exit restoration,
and no JavaScript or WebGL errors. The opt-in fit screenshot exposes a teal
relation network that is hard to see in the default screenshot, but the 7/4
dash rhythm is still not reliably readable. The two screenshots came from
separately moving pages, not an identical paused position/camera, so they are
not a controlled pixel A/B. The hover picked another valid point than the
harness's intended target, which is not exact-target acquisition proof.

At this initial smoke, no same-geometry visual comparison, physical 12-second
diagnostic, complete three-by-60-second physical gate, or browser Task Manager
GPU-memory sample had been performed for this variant. It remained a
diagnostic, not adopted V0 rendering or permission to start F14′/V2. Ignored
report and four PNGs:
`shader-dim-probe/results/citation-foreground-headless-2026-09-28T07-48-02Z.json`
(report SHA-256 `9b26c204055feac99b87e6896bce541581f92e4b9a1f96bef085bdbb2840fbed`).

## Rejected citation foreground after controlled and physical checks (2026-09-28)

A follow-up paused the same 20,000-point/47,000-link scene and reapplied
identical positions and camera to default and foreground captures. Corpus,
layout, link endpoint/style/order, native color/width buffer, position, and
camera hashes matched; the only requested mode difference was the opt-in
foreground. Across 657 visible 30–60 px citation candidates, the selected
lower-decile 35.59 px line still had 61 crossings and 85 near-line encounters;
the median 43.39 px line had 100 and 122. The opt-in increased teal pixels
throughout both native-size crops, but neither selected 7/4-pixel dash became
reliably traceable. This was a SwiftShader software-rendered visual diagnostic,
not a physical performance test or a style approval. Ignored report and crop
manifest:
`shader-dim-probe/results/citation-foreground-same-geometry-2026-09-28T08-00-36-281Z.json`
(SHA-256 `25b8a34a93f59d5bc2c8b5807c7cffdc5ee9ae852d4717e72eeee4885c300d89`).

A separate source-frozen, sequential **A/B/A** on Chrome 153 / Intel UHD 770
D3D11 used the same star-only/global-point-dim/design-fade build and synthetic
corpus/layout/CSR hashes in all three legs. After a five-second warmup, each
leg sampled 12 seconds of moving 20,000 points and 47,000 native links. The
default / foreground / default legs recorded **561 / 358 / 550 frames**,
frame-interval p50 **17.7 / 36.0 / 18.0 ms**, and p95 **35.8 / 36.1 / 36.0 ms**.
Simulation ticks were 562 / 358 / 550; foreground draws were 0 / 358 / 0.
The foreground leg's frame count was about **35.6% lower** than the mean of
the flanking controls, and the recovery in the final control supports a
render-cost attribution within this short protocol. There were no observed
JavaScript, request, HTTP, adapter, or WebGL errors or >50 ms main-thread long
tasks. These p95 values do not exceed the 50 ms threshold; the throughput loss
and failed visual check are separate reasons not to adopt this candidate.

This new A/B/A used `designLinkFade=1` and therefore is not a direct replay
of the earlier three-by-60-second full-gate baseline. The legs had the same
inputs and bundle but live positions and browser conditions were not held
identical. No browser Task Manager GPU-memory sample, full three-by-60-second
gate, or physical style approval was obtained. The optional foreground remains
off by default and rejected as a V0 visual solution; F14′ and V2 remain closed.
Ignored physical report:
`shader-dim-probe/results/citation-foreground-physical-short-2026-09-28T08-00-33-422Z-39d43996183e.json`
(SHA-256 `21c296d59bd5178130a0ee308dbf4f0dd27ad9cfb60b8dd5e83dd4e34b7c9566`).

## Local V0 contract versus harness (2026-09-28)

The locally available September 27 replan is a **three-round draft**, not
proof of the later five-round final document; that final page's unauthenticated
read API returns 401 in this environment. The draft's V0-T3 lists the
physical 20,000-point/47,000-link timing, worker, WebGL-memory and browser
Task Manager GPU-memory limits. The current full-gate evaluator also requires
hash-bound manual visual/style approval, a separate 60-second hovered window,
and in-window winner sampling. Those are conservative evidence policies, not
word-for-word V0-T3 numeric conditions. The teal 7/4 dash itself remains a
draft-wide design requirement, so its unreadability cannot be called a
finished UI merely by separating it from T3.

The draft does **not** prescribe the Map's 0.5 px link width; treating 1 px as
a plan deviation is a harness convention. Conversely, its general performance
rule calls for three interleaved before/after measurements and a median,
whereas the current full-gate runs the same variant three times and judges
each run. Neither protocol should be described as the other. Even a fresh
GPU-memory sample within limit would only establish the quantitative T3
subconditions until the visual evidence, full Node 22 CI/desktop build, and
authoritative final plan are resolved. Past browser-memory windows cannot be
filled retrospectively.

## Rejected superlinear zoom-fade diagnostic (2026-09-28)

The pinned Cosmos link shader fades **long** projected links: its opacity
factor falls from one at the near range to the 3% floor beyond the far range.
The existing opt-in design fade scales its `[10, 60]` px range linearly with
zoom, which leaves a fixed edge at nearly the same attenuation. A further
ignored, default-off `fadeZoomExponent=1.5` diagnostic scaled that range
superlinearly as a specific test of the draft's “hover/zoom makes links
clearer” visual requirement. The default exponent remains one. Node 22.18
unit tests for the camera rule and crossing selector passed 5/5.

The paused SwiftShader A/B held the same 20,000 points, 47,000 native links,
positions, cameras, link order, colors, widths, style buffer, 7/4 dash, and
3% fade floor. At 3× zoom the range changed from `[30, 180]` to
`[51.96, 311.77]` px. The selector first ranked **2,655** visible fit-view
30–60 px citation candidates by geometric encounters, then followed its
lower-decile **32.11 px** and median **48.05 px** citations to **96.34** and
**144.16 px** at 3× zoom. Both were offscreen in the unpanned detail camera;
the probe recorded that fact and centered each original edge separately in
matched A/B cameras. Their fit-view crossing counts were **105** and **177**.
Two detail-camera-selected citations were also captured as supplementary
checks. Source, bundle, corpus, layout, native visual-buffer, position, and
camera hashes and error checks are in the ignored report.

The larger range exposed more faint surrounding links, but independent
inspection of all four fit-selected native-size crops still could not trace
the selected citation's individual 7/4 dash rhythm. Pixel differences alone
do not establish legibility. This variant is rejected as a visual solution;
it was never a moving physical-GPU measurement or a three-by-60-second V0
gate, and it supplies no browser Task Manager GPU-memory value. The probe
remains opt-in and does not authorize F14′ or V2. Ignored report and PNGs:
`shader-dim-probe/results/zoom-fade-same-geometry-2026-09-28T11-36-16-862Z.json`
(SHA-256 `e072271d85994563cc2530459b341b7fbc21f7b6929c69e768fc63f169c57c57`).

## Rejected camera-refit and citation-casing diagnostics (2026-09-28)

A paused SwiftShader camera A/B froze a 60-second live-layout snapshot and
changed only the camera. One `fitView` enlarged the 20,000-point cloud from
481×488 to 674×684 px at 1440×900 while keeping all points onscreen; the
zoom changed from 0.391957 to 0.549222. Link identities, styles, colors,
widths, point properties, and positions were hash-equal. The low- and
median-ranked citation segments grew from 30.61/32.98 to 42.89/46.21 px,
but their 47/207 geometric crossings were unchanged. Native-size crops
still did not expose either individual 7/4 dash rhythm. The snapshot had
only 143 simulation ticks and alpha 0.20575, so it was not a settled-layout
or moving physical-GPU gate result. Ignored report and images:
`shader-dim-probe/results/camera-refit-same-geometry-2026-09-28T11-50-59-185Z.json`
(SHA-256 `976f2f29068e8c617e0bfb81ef91271303cb164fb3ec0485f8605163e5f3ed74`).

A separate, default-off pinned-shader casing probe kept the same 20,000
points, 47,000 links, frozen positions, endpoint/style/order/color buffers,
and fit/detail cameras across three arms: original 0.5 px citation,
1.5 px width-only, and matched-width 1.5 px casing. The casing altered only
the citation's **on-segment** fringe; the teal 7/4 core and off-segment mask
remained. Direct review of the native-size low- and median-ranked crops found
no reliably traceable target dash rhythm. Against the matched width-only
control, casing reduced rather than improved the detail crop's +15 teal
pixel count (26→15 and 16→12) and summed teal excess (1146→863 and
1322→948). A +15 absolute threshold alone is not a fair design gate: under
the prescribed alpha/fade, the median 48 px citation's unobscured maximum
teal excess is about 12.8 before antialiasing. This candidate is rejected
on the combined visual and relative-signal evidence, not that impossible
threshold. Node 22.18 focused tests passed 8/8 and the unchanged default
shader hashes verified. It was neither a light-theme test nor a physical-GPU
performance or memory run. Ignored report and images:
`shader-dim-probe/results/citation-casing-same-geometry-2026-09-28T11-51-35-547Z.json`
(SHA-256 `1269ded0ce334d1b9c30ce4ffcb9c8da620eda21040dece12f145172c864b7d3`).

At code snapshot `f8d53e3a`, local Node 22.18 frontend typecheck, lint,
i18n coverage (15 namespaces, 4,639 English keys), 2,194 unit tests, and
production build passed. Desktop tests passed 26/26, and Linux Electron
`--dir` packaging passed after installing missing local dependencies. The built
archive contains `src/main.cjs` and `electron-updater`, with the bundled
`dtxwiki-engine` resource present. These local checks do not establish
all-CI green, Windows/macOS desktop packaging, the visual
design decision, or the missing in-window browser Task Manager GPU-memory
measurement. V0 remains unqualified and F14′/V2 remain closed.

## Node 22.18 local E2E follow-up (2026-09-28)

At code snapshot `4190c705`, the standard E2E entry point ran under Node
22.18.0 with ambient `DTXWIKI_DATA_DIR`, `DTXWIKI_MCP_PAT`, and profile
overrides removed. It migrated a newly created, mode-0700 temporary SQLite
database through `0002_epic1`, built its own single-origin frontend bundle,
and completed **142/142 Playwright tests** in 9.3 minutes (Chromium suite plus
tagged Firefox/WebKit cross-browser cases). Exit status was zero, and the
specific temporary DB directory was absent afterward. This strengthens the
local V0-T1 toolchain evidence, but is not a remote all-CI-green result and
does not address V0-T3's missing physical GPU memory or visual decision.
