# Head-to-head round 5: ten held-out topics (topics fixed 2026-10-18, before any key)

Round 4 has five tasks, and no sign test over five tasks can go below p = 0.0625.
Round 5 asks the same question ("find every public dataset that matches a need") on
ten new topics, chosen across fields and archive types before any key was built and
before looking at which archives this server covers. Each has a backup, used only if
the primary fails the size gate (5 to 30 firm studies in at least 3 archives).

| task | need (primary) | backup |
|---|---|---|
| V1 | spatial transcriptomics (Visium, Slide-seq, MERFISH, Stereo-seq, GeoMx...) of the inner ear or cochlea, any vertebrate | spatial transcriptomics of the human placenta |
| V2 | chromatin accessibility or chromatin-binding assays (ATAC-seq, DNase-seq, ChIP-seq, CUT&RUN/CUT&Tag) of cnidarians (corals, sea anemones, Hydra, jellyfish) | the same assays in sponges, ctenophores or placozoans |
| V3 | brain imaging or recording (MRI, fMRI, DTI, EEG, MEG, fNIRS) of people who stutter | brain imaging of congenitally or early blind people |
| V4 | carcass-search or collision-monitoring data for birds or bats at wind turbines | bird collisions with buildings or glass |
| V5 | mass-spectrometry or NMR metabolomics or lipidomics of human breast milk | metabolomics or lipidomics of honey |
| V6 | ice-core records of black carbon (refractory BC, soot) | ice-core records of methanesulfonic acid (MSA) |
| V7 | annotated aerial, drone or satellite images of whales made for detection or counting | the same for seals and other pinnipeds |
| V8 | speech or voice recordings of people with Parkinson's disease, for acoustic analysis | speech recordings of people with ALS or other dysarthria |
| V9 | metagenomic or amplicon (16S/ITS) sequencing of hospital sink drains, P-traps or other hospital wastewater plumbing | the same for household dishwashers or washing machines |
| V10 | ancient DNA sequenced from the dental calculus of non-human animals | ancient DNA from coprolites |

Keys are built under `~/data-aggregator-mcp-artifacts/h2h/round5/keys/`, outside the
repository, by builders that have neither this server, nor its code, nor ToolUniverse
(see `../PREREG_2026-10-18.md`). Their sha256 values are committed before the sweep and
the keys themselves after it, so the web arm cannot read them during the run.
