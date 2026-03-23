# Kit location: use outside the Play repo

This folder is the **Play-to-Spring Migration Kit**. It may live inside a Play repo for versioning/reference only.

**To use it**, copy the entire `play-to-spring-kit` folder to a place **outside** any Play repo, then run setup from there:

```bash
# From the parent of your Play repo
cp -r <play-repo>/play-to-spring-kit .
cd play-to-spring-kit
./scripts/setup.sh ../<play-repo>
```

Or clone/copy the kit to its own repo and run:

```bash
./scripts/setup.sh /path/to/your-play-repo
```

That way the kit stays independent and works for any Play project; setup creates `spring-<basename>` and copies skills/metadata into the Play repo you pass.
