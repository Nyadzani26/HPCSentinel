# Collecting baseline data on a cluster that is not on 24/7

The detector is unsupervised and learns only from real, normal behaviour of the cluster it monitors. On the
SPU Ranger test cluster this needs care for two reasons.

1. Ranger is switched on at about 10:00 and off at about 17:00 because the cooling is not stable. The
   data therefore has daytime windows only, with a cold-start period after each boot. The platform handles
   this as designed: windows with missing or too little data are marked unreliable and are never scored
   (BR-02), and the first windows after a boot are skipped until enough history exists.
2. An idle cluster teaches only the idle baseline. A single uniform job teaches a narrow loaded baseline that
   real jobs then violate. Real simulations differ in what they stress, so the loaded state must be learned
   from several kinds of normal work.

`tools/daily_workload.py` runs once per day inside the switched-on window. It plans a random but reproducible
mix of normal job profiles (processor-bound, memory-heavy, I/O-heavy, communication-heavy, mixed) across random
nodes, never loading more than 60 percent of the nodes at once so idle peers always exist, and submits it to Slurm.
It injects no faults. The plan and the actual start and end of every job are written to
`baseline-plan-DATE.csv` and `baseline-workload-DATE.csv`, so the composition of the learning data is documented.

    # on the head node, once per day after the nodes have booted
    25 10 * * * /home/gift/HPCSentinel/tools/daily_workload.py --until 16:30 >> /home/gift/HPCSentinel/daily_workload.log 2>&1

Faults (`tools/run_faults.sh`) are run on separate days or after the learning period and are logged to a CSV,
so they can be excluded from learning (Periods to ignore) and used for evaluation.

`manage.py inventory` reports how many idle and loaded training rows each node has, and whether that is enough
for the detector's minimum row counts, before a model is trained.

Synthetic data is never used to train the detector. Controlled anomalies injected into real healthy windows are
used only to evaluate it.
