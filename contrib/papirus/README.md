# Papirus icon

[Papirus](https://github.com/PapirusDevelopmentTeam/papirus-icon-theme)-style
Tideway icons, contributed by [@C-O-D](https://github.com/J-M-PUNK/tideway/issues/317)
(#317): the Papirus circular base with Tideway's equalizer mark in the app's
cyan. Three size-tuned versions are included — Papirus renders app icons
differently at small sizes, so 16 / 24 / 32 px each get their own drawing.

```
16x16/apps/com.tidaldownloader.Tideway.svg
24x24/apps/com.tidaldownloader.Tideway.svg
32x32/apps/com.tidaldownloader.Tideway.svg
```

## Using it

The desktop entry ships `Icon=com.tidaldownloader.Tideway`, so the files here
are named to match. The folders mirror Papirus's own tree, so you can copy them
straight in and refresh the icon cache:

```sh
sudo cp -r 16x16 24x24 32x32 /usr/share/icons/Papirus/
sudo gtk-update-icon-cache /usr/share/icons/Papirus
```

To keep the icons after Papirus updates, copy them into a personal theme dir
(`~/.local/share/icons/…`) instead.

Thanks to @C-O-D for the artwork.
