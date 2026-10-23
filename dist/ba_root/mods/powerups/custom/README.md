# Adding a future modded powerup

1. Put the custom implementation in this folder, for example `lightning.py`.
2. Register the type from that module:

```python
from mods.powerups.registry import register_powerup


def register():
    register_powerup("lightning", "Lightning", factory=LightningPowerup)
```

3. Add the corresponding entry to `mods/powerups/powerups.json`:

```json
"lightning": {
    "enabled": true,
    "quantity": 2,
    "display_name": "Lightning"
}
```

4. Your custom implementation must create its own visuals, pickup behavior, and
   effect. After creating its scene node, call the shared manager to count it
   and add the same label/countdown display:

```python
from mods.powerups.manager import get_manager

manager = get_manager()
if manager is not None:
    manager.track_box(
        self,
        "lightning",
        self.node,
        lifetime_seconds=DEFAULT_POWERUP_INTERVAL - 1.0,
    )
```

Call `manager.untrack_box(self)` when the custom powerup is collected or
removed. Check `manager.can_spawn("lightning")` before constructing the box.

Registering a name alone does not implement a new powerup. The built-in
`PowerupBox` only understands the nine stock types; genuinely new types need a
custom actor/effect implementation that cooperates with this manager.
