package main

import (
	"flag"
	"log"
	"os"

	"orf-go/internal/ui"

	"fyne.io/fyne/v2/app"
)

func main() {
	defaultRoot, err := os.Getwd()
	if err != nil {
		log.Fatal(err)
	}
	root := flag.String("root", defaultRoot, "root folder to browse")
	flag.Parse()

	a := app.NewWithID("orf-go")
	ui.NewApp(a, *root).Run()
}
