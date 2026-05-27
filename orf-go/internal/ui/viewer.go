package ui

import (
	"bytes"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"math"
	"path/filepath"
	"strconv"

	"orf-go/internal/openers"
	"orf-go/internal/preview"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/widget"
)

func showViewer(parent fyne.Window, path string, store *openers.Store) {
	data, err := preview.LoadPreviewBytes(path)
	if err != nil {
		dialog.ShowError(err, parent)
		return
	}
	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		dialog.ShowError(err, parent)
		return
	}

	win := fyne.CurrentApp().NewWindow(filepath.Base(path))
	win.Resize(fyne.NewSize(1100, 800))

	view := newZoomImage(img)
	scaleLabel := widget.NewLabel("100%")
	view.onZoom = func(percent int) {
		scaleLabel.SetText(formatPercent(percent))
	}

	fit := widget.NewButton("Fit to window", view.fitToWindow)
	open := widget.NewButton("Open", func() {
		if err := store.Open(path); err != nil {
			dialog.ShowError(err, win)
		}
	})
	top := container.NewBorder(nil, nil, widget.NewLabel(filepath.Base(path)), container.NewHBox(fit, scaleLabel, open))
	win.SetContent(container.NewBorder(top, nil, nil, nil, view))
	win.Show()
}

func formatPercent(percent int) string {
	if percent < 1 {
		percent = 1
	}
	return strconv.Itoa(percent) + "%"
}

type zoomImage struct {
	widget.BaseWidget
	img      *canvas.Image
	orig     fyne.Size
	scale    float32
	offset   fyne.Position
	onZoom   func(int)
	hasImage bool
	fitted   bool
}

func newZoomImage(img image.Image) *zoomImage {
	z := &zoomImage{
		img:      canvas.NewImageFromImage(img),
		orig:     fyne.NewSize(float32(img.Bounds().Dx()), float32(img.Bounds().Dy())),
		scale:    1,
		hasImage: true,
	}
	z.img.FillMode = canvas.ImageFillContain
	z.ExtendBaseWidget(z)
	return z
}

func (z *zoomImage) CreateRenderer() fyne.WidgetRenderer {
	return &zoomImageRenderer{z: z, objects: []fyne.CanvasObject{z.img}}
}

func (z *zoomImage) MinSize() fyne.Size {
	return fyne.NewSize(320, 240)
}

func (z *zoomImage) Scrolled(event *fyne.ScrollEvent) {
	if !z.hasImage {
		return
	}
	if event.Scrolled.DY > 0 {
		z.scale *= 1.2
	} else {
		z.scale /= 1.2
	}
	z.scale = float32(math.Max(0.05, math.Min(20, float64(z.scale))))
	z.emitZoom()
	z.Refresh()
}

func (z *zoomImage) Dragged(event *fyne.DragEvent) {
	z.offset = fyne.NewPos(z.offset.X+event.Dragged.DX, z.offset.Y+event.Dragged.DY)
	z.Refresh()
}

func (z *zoomImage) DragEnd() {}

func (z *zoomImage) fitToWindow() {
	if !z.hasImage {
		return
	}
	z.applyFit(z.Size())
	z.fitted = true
	z.emitZoom()
	z.Refresh()
}

func (z *zoomImage) emitZoom() {
	if z.onZoom != nil {
		z.onZoom(int(math.Round(float64(z.scale * 100))))
	}
}

func (z *zoomImage) applyFit(size fyne.Size) {
	if size.Width <= 0 || size.Height <= 0 || z.orig.Width <= 0 || z.orig.Height <= 0 {
		return
	}
	z.scale = float32(math.Min(float64(size.Width/z.orig.Width), float64(size.Height/z.orig.Height)))
	z.offset = fyne.NewPos((size.Width-z.orig.Width*z.scale)/2, (size.Height-z.orig.Height*z.scale)/2)
}

type zoomImageRenderer struct {
	z       *zoomImage
	objects []fyne.CanvasObject
}

func (r *zoomImageRenderer) Layout(size fyne.Size) {
	if !r.z.fitted {
		r.z.applyFit(size)
		r.z.fitted = true
		r.z.emitZoom()
	}
	r.z.img.Resize(fyne.NewSize(r.z.orig.Width*r.z.scale, r.z.orig.Height*r.z.scale))
	r.z.img.Move(r.z.offset)
}

func (r *zoomImageRenderer) MinSize() fyne.Size {
	return r.z.MinSize()
}

func (r *zoomImageRenderer) Refresh() {
	r.Layout(r.z.Size())
	canvas.Refresh(r.z.img)
}

func (r *zoomImageRenderer) Objects() []fyne.CanvasObject {
	return r.objects
}

func (r *zoomImageRenderer) Destroy() {}
