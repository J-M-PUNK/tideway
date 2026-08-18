# Papirus icon

A [Papirus](https://github.com/PapirusDevelopmentTeam/papirus-icon-theme)-style
Tideway icon, contributed by [@C-O-D](https://github.com/J-M-PUNK/tideway/issues/317)
(#317). It follows the Papirus circular base with Tideway's equalizer mark in
the app's cyan, so it sits consistently alongside other Papirus app icons.

## Using it

The desktop entry ships `Icon=com.tidaldownloader.Tideway`, so the file here is
named to match. Drop it into your Papirus theme's app-icon directory and refresh
the icon cache, for example:

```sh
sudo cp com.tidaldownloader.Tideway.svg \
  /usr/share/icons/Papirus/48x48/apps/
sudo gtk-update-icon-cache /usr/share/icons/Papirus
```

Papirus resolves an SVG placed in any one size bucket across every size, so a
single copy is enough. To keep it after Papirus updates, drop it in a personal
theme dir (`~/.local/share/icons/…`) instead.

Thanks to @C-O-D for the contribution.
