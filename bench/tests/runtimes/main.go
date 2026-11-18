// Copy a file, send it over a local socket, copy one connection into another and serve it over HTTP,
// each through the library call a Go program would use.
package main

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"
)

func must(err error) {
	if err != nil {
		panic(err)
	}
}

func drain(l net.Listener, done chan<- bool) {
	c, err := l.Accept()
	must(err)
	out, err := os.Create("got.bin")
	must(err)
	_, err = io.Copy(out, c)
	must(err)
	must(out.Close())
	done <- true
}

func main() {
	switch os.Args[1] {
	case "copy":
		in, err := os.Open("in.bin")
		must(err)
		out, err := os.Create("out.bin")
		must(err)
		_, err = io.Copy(out, in)
		must(err)
		must(out.Close())
	case "send":
		l, err := net.Listen("unix", "sock")
		must(err)
		done := make(chan bool)
		go drain(l, done)
		in, err := os.Open("in.bin")
		must(err)
		c, err := net.Dial("unix", "sock")
		must(err)
		_, err = io.Copy(c, in)
		must(err)
		must(c.Close())
		<-done
	case "relay":
		sink, err := net.Listen("tcp", "127.0.0.1:0")
		must(err)
		done := make(chan bool)
		go drain(sink, done)
		first, err := net.Listen("tcp", "127.0.0.1:0")
		must(err)
		go func() {
			c, err := net.Dial("tcp", first.Addr().String())
			must(err)
			in, err := os.Open("in.bin")
			must(err)
			data, err := io.ReadAll(in)
			must(err)
			_, err = c.Write(data)
			must(err)
			must(c.Close())
		}()
		from, err := first.Accept()
		must(err)
		to, err := net.Dial("tcp", sink.Addr().String())
		must(err)
		_, err = io.Copy(to, from)
		must(err)
		must(to.Close())
		<-done
	default:
		mux := http.NewServeMux()
		mux.HandleFunc("GET /in.bin", func(w http.ResponseWriter, r *http.Request) { http.ServeFile(w, r, "in.bin") })
		mux.HandleFunc("GET /quit", func(w http.ResponseWriter, _ *http.Request) {
			fmt.Fprint(w, "bye")
			go func() { time.Sleep(300 * time.Millisecond); os.Exit(0) }()
		})
		must(http.ListenAndServe("127.0.0.1:18080", mux))
	}
	fmt.Println("ok")
}
