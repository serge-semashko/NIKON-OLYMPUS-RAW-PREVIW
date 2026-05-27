package ui

import (
	"bytes"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"

	"orf-go/internal/openers"
	"orf-go/internal/preview"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

type fileItem struct {
	Name string
	Path string
	Dir  bool
}

type App struct {
	fyneApp fyne.App
	win     fyne.Window

	root         string
	currentPath  string
	mode         string
	sizeName     string
	selectedPath string
	loadToken    atomic.Int64

	driveSelect *widget.Select
	tree        *widget.Tree
	pathLabel   *widget.Label
	progress    *widget.ProgressBar
	content     *fyne.Container

	itemsMu     sync.Mutex
	items       []fileItem
	thumbImages map[string]*canvas.Image
	thumbTotal  atomic.Int64
	thumbDone   atomic.Int64

	openers *openers.Store
}

func NewApp(a fyne.App, root string) *App {
	absRoot, err := filepath.Abs(root)
	if err != nil {
		absRoot = root
	}
	cfg := filepath.Join(".", "opener_settings.json")
	return &App{
		fyneApp:     a,
		root:        absRoot,
		currentPath: absRoot,
		mode:        "preview",
		sizeName:    "big",
		thumbImages: map[string]*canvas.Image{},
		openers:     openers.Load(cfg),
	}
}

func (a *App) Run() {
	a.win = a.fyneApp.NewWindow("ORF Explorer Go")
	a.win.Resize(fyne.NewSize(1280, 820))
	a.buildUI()
	a.loadFolder(a.root)
	a.win.ShowAndRun()
}

func (a *App) buildUI() {
	drives := listDrives()
	currentDrive := driveRoot(a.root)
	if currentDrive == "" && len(drives) > 0 {
		currentDrive = drives[0]
	}

	a.driveSelect = widget.NewSelect(drives, func(value string) {
		if value == "" {
			return
		}
		a.tree.Root = value
		a.tree.Refresh()
		a.tree.OpenBranch(value)
		a.loadFolder(value)
	})
	a.driveSelect.SetSelected(currentDrive)

	a.tree = widget.NewTree(
		func(uid string) []string { return childDirs(uid) },
		func(uid string) bool { return isDir(uid) },
		func(branch bool) fyne.CanvasObject { return widget.NewLabel("folder") },
		func(uid string, branch bool, obj fyne.CanvasObject) {
			label := obj.(*widget.Label)
			if uid == a.tree.Root {
				label.SetText(uid)
				return
			}
			label.SetText(filepath.Base(uid))
		},
	)
	a.tree.Root = currentDrive
	a.tree.HideSeparators = true
	a.tree.OnSelected = func(uid string) {
		a.loadFolder(uid)
	}
	a.tree.OpenBranch(currentDrive)

	left := container.NewBorder(
		container.NewVBox(widget.NewLabel("Drive:"), a.driveSelect),
		nil, nil, nil,
		container.NewVScroll(a.tree),
	)

	a.pathLabel = widget.NewLabel("/")
	tableBtn := widget.NewButton("Table", func() {
		a.mode = "table"
		a.renderContent()
	})
	previewBtn := widget.NewButton("Preview", func() {
		a.mode = "preview"
		a.renderContent()
	})
	size := widget.NewSelect([]string{"Small", "Big", "Large"}, func(value string) {
		a.sizeName = strings.ToLower(value)
		a.renderContent()
		a.queueThumbnails(a.loadToken.Load())
	})
	size.SetSelected("Big")
	openBtn := widget.NewButton("Open", a.openSelected)
	settingsBtn := widget.NewButton("Open settings", a.showOpenSettings)

	top := container.NewBorder(
		nil, nil,
		container.NewHBox(widget.NewLabel("Current folder:"), a.pathLabel),
		container.NewHBox(tableBtn, previewBtn, widget.NewLabel("Size:"), size, openBtn, settingsBtn),
	)

	a.progress = widget.NewProgressBar()
	a.progress.Hide()
	a.content = container.NewStack()
	right := container.NewBorder(container.NewVBox(top, a.progress), nil, nil, nil, a.content)

	split := container.NewHSplit(left, right)
	split.Offset = 0.25
	a.win.SetContent(split)
}

func (a *App) loadFolder(path string) {
	token := a.loadToken.Add(1)
	a.currentPath = path
	a.pathLabel.SetText(path)
	a.progress.Show()
	a.progress.SetValue(0)

	entries, err := readEntries(path)
	if err != nil {
		a.progress.Hide()
		dialog.ShowError(err, a.win)
		return
	}

	a.itemsMu.Lock()
	a.items = entries
	a.thumbImages = map[string]*canvas.Image{}
	a.itemsMu.Unlock()

	a.renderContent()
	a.queueThumbnails(token)
}

func (a *App) renderContent() {
	a.itemsMu.Lock()
	items := append([]fileItem(nil), a.items...)
	a.itemsMu.Unlock()

	if a.mode == "table" {
		a.renderTable(items)
		return
	}
	a.renderGrid(items)
}

func (a *App) renderTable(items []fileItem) {
	list := widget.NewList(
		func() int { return len(items) },
		func() fyne.CanvasObject {
			return container.NewHBox(widget.NewIcon(theme.DocumentIcon()), widget.NewLabel("name"))
		},
		func(id widget.ListItemID, obj fyne.CanvasObject) {
			row := obj.(*fyne.Container)
			icon := row.Objects[0].(*widget.Icon)
			label := row.Objects[1].(*widget.Label)
			item := items[id]
			if item.Dir {
				icon.SetResource(theme.FolderIcon())
			} else if preview.CanPreview(item.Path) {
				icon.SetResource(theme.FileImageIcon())
			} else {
				icon.SetResource(theme.DocumentIcon())
			}
			label.SetText(item.Name)
		},
	)
	list.OnSelected = func(id widget.ListItemID) {
		if id < 0 || id >= len(items) {
			return
		}
		a.activate(items[id])
		list.Unselect(id)
	}
	a.content.Objects = []fyne.CanvasObject{list}
	a.content.Refresh()
}

func (a *App) renderGrid(items []fileItem) {
	size := a.thumbSize()
	cards := make([]fyne.CanvasObject, 0, len(items))
	for _, item := range items {
		img := canvas.NewImageFromResource(theme.DocumentIcon())
		img.FillMode = canvas.ImageFillContain
		img.SetMinSize(size)
		if item.Dir {
			img.Resource = theme.FolderIcon()
		} else if preview.CanPreview(item.Path) {
			img.Resource = theme.FileImageIcon()
			a.thumbImages[item.Path] = img
		}
		name := widget.NewButton(item.Name, func(it fileItem) func() {
			return func() { a.activate(it) }
		}(item))
		card := container.NewVBox(img, name)
		cards = append(cards, card)
	}
	grid := container.NewGridWrap(fyne.NewSize(size.Width+60, size.Height+60), cards...)
	a.content.Objects = []fyne.CanvasObject{container.NewVScroll(grid)}
	a.content.Refresh()
}

func (a *App) activate(item fileItem) {
	a.selectedPath = item.Path
	if item.Dir {
		a.loadFolder(item.Path)
		return
	}
	if preview.CanPreview(item.Path) {
		showViewer(a.win, item.Path, a.openers)
	}
}

func (a *App) openSelected() {
	path := a.selectedPath
	if path == "" {
		dialog.ShowInformation("Open", "Select a file first.", a.win)
		return
	}
	if info, err := os.Stat(path); err != nil || info.IsDir() {
		dialog.ShowInformation("Open", "Select a file (not a folder).", a.win)
		return
	}
	if err := a.openers.Open(path); err != nil {
		dialog.ShowError(err, a.win)
	}
}

func (a *App) showOpenSettings() {
	labels := []struct {
		key   string
		title string
	}{
		{".orf", "ORF files"},
		{".nef", "NEF files"},
		{".jpg", "JPG files"},
		{".jpeg", "JPEG files"},
		{".png", "PNG files"},
		{".tif", "TIF files"},
		{".tiff", "TIFF files"},
		{".psd", "PSD files"},
		{"*", "Other files (fallback)"},
	}
	entries := map[string]*widget.Entry{}
	rows := []fyne.CanvasObject{
		widget.NewLabel("Leave empty to use the system default app."),
	}
	for _, item := range labels {
		entry := widget.NewEntry()
		entry.SetText(a.openers.Values[item.key])
		entry.SetPlaceHolder("Path to program (.exe) or empty")
		key := item.key
		browse := widget.NewButton("Browse", func() {
			dialog.ShowFileOpen(func(reader fyne.URIReadCloser, err error) {
				if err != nil {
					dialog.ShowError(err, a.win)
					return
				}
				if reader == nil {
					return
				}
				defer reader.Close()
				entry.SetText(uriPath(reader.URI().Path()))
			}, a.win)
		})
		rows = append(rows, container.NewBorder(nil, nil, widget.NewLabel(item.title), browse, entry))
		entries[key] = entry
	}
	reset := widget.NewButton("Reset to defaults", func() {
		defaults := openers.Defaults()
		for key, entry := range entries {
			entry.SetText(defaults[key])
		}
	})
	rows = append(rows, reset)

	dialog.ShowCustomConfirm("Open settings", "Save", "Cancel", container.NewVScroll(container.NewVBox(rows...)), func(save bool) {
		if !save {
			return
		}
		for key, entry := range entries {
			a.openers.Values[key] = strings.TrimSpace(entry.Text)
		}
		if err := a.openers.Save(); err != nil {
			dialog.ShowError(err, a.win)
		}
	}, a.win)
}

func (a *App) queueThumbnails(token int64) {
	a.itemsMu.Lock()
	targets := make([]string, 0)
	for _, item := range a.items {
		if !item.Dir && preview.CanPreview(item.Path) {
			targets = append(targets, item.Path)
		}
	}
	a.itemsMu.Unlock()

	a.thumbTotal.Store(int64(len(targets)))
	a.thumbDone.Store(0)
	if len(targets) == 0 {
		a.progress.Hide()
		return
	}
	a.progress.Show()
	a.progress.SetValue(0)

	for _, path := range targets {
		go a.loadThumbnail(token, path)
	}
}

func (a *App) loadThumbnail(token int64, path string) {
	data, err := preview.LoadPreviewBytes(path)
	done := a.thumbDone.Add(1)
	total := a.thumbTotal.Load()
	if token != a.loadToken.Load() {
		return
	}
	if err != nil {
		fyne.Do(func() {
			a.updateProgress(done, total)
		})
		return
	}
	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		fyne.Do(func() {
			a.updateProgress(done, total)
		})
		return
	}
	fyne.Do(func() {
		if target, ok := a.thumbImages[path]; ok {
			target.Image = img
			target.Resource = nil
			target.Refresh()
		}
		a.updateProgress(done, total)
	})
}

func (a *App) updateProgress(done, total int64) {
	if total <= 0 {
		a.progress.Hide()
		return
	}
	a.progress.SetValue(float64(done) / float64(total))
	if done >= total {
		a.progress.Hide()
	}
}

func (a *App) thumbSize() fyne.Size {
	switch a.sizeName {
	case "small":
		return fyne.NewSize(120, 90)
	case "large":
		return fyne.NewSize(260, 195)
	default:
		return fyne.NewSize(180, 135)
	}
}

func readEntries(dir string) ([]fileItem, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	items := make([]fileItem, 0, len(entries))
	for _, entry := range entries {
		full := filepath.Join(dir, entry.Name())
		items = append(items, fileItem{Name: entry.Name(), Path: full, Dir: entry.IsDir()})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].Dir != items[j].Dir {
			return items[i].Dir
		}
		return strings.ToLower(items[i].Name) < strings.ToLower(items[j].Name)
	})
	return items, nil
}

func childDirs(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var dirs []string
	for _, entry := range entries {
		if entry.IsDir() {
			dirs = append(dirs, filepath.Join(dir, entry.Name()))
		}
	}
	sort.Slice(dirs, func(i, j int) bool {
		return strings.ToLower(filepath.Base(dirs[i])) < strings.ToLower(filepath.Base(dirs[j]))
	})
	return dirs
}

func isDir(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

func listDrives() []string {
	if runtime.GOOS != "windows" {
		return []string{string(filepath.Separator)}
	}
	var drives []string
	for letter := 'A'; letter <= 'Z'; letter++ {
		drive := fmt.Sprintf("%c:\\", letter)
		if _, err := os.Stat(drive); err == nil {
			drives = append(drives, drive)
		}
	}
	return drives
}

func driveRoot(path string) string {
	volume := filepath.VolumeName(path)
	if volume == "" {
		return string(filepath.Separator)
	}
	return volume + string(filepath.Separator)
}

func uriPath(path string) string {
	if runtime.GOOS == "windows" && strings.HasPrefix(path, "/") && len(path) > 3 && path[2] == ':' {
		return path[1:]
	}
	return path
}
