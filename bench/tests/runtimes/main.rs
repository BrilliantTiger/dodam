//! Copy a file, send it over a local socket, copy one connection into another and serve it over
//! HTTP, each through the library call a Rust program would use.
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;

use axum::Router;
use axum::routing::get;

fn drain(mut from: UnixStream) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let mut out = File::create("got.bin").unwrap();
        io::copy(&mut from, &mut out).unwrap();
    })
}

fn main() {
    let mode = std::env::args().nth(1).unwrap();
    match mode.as_str() {
        "copy" => {
            std::fs::copy("in.bin", "out.bin").unwrap();
        }
        "send" => {
            let (mut a, b) = UnixStream::pair().unwrap();
            let taking = drain(b);
            let mut source = File::open("in.bin").unwrap();
            io::copy(&mut source, &mut a).unwrap();
            drop(a);
            taking.join().unwrap();
        }
        "relay" => {
            let (mut a1, mut a2) = UnixStream::pair().unwrap();
            let (mut b1, b2) = UnixStream::pair().unwrap();
            let taking = drain(b2);
            let feeding = std::thread::spawn(move || {
                let mut data = Vec::new();
                File::open("in.bin").unwrap().read_to_end(&mut data).unwrap();
                a1.write_all(&data).unwrap();
            });
            io::copy(&mut a2, &mut b1).unwrap();
            drop(b1);
            feeding.join().unwrap();
            taking.join().unwrap();
        }
        _ => {
            tokio::runtime::Runtime::new().unwrap().block_on(async {
                let router = Router::new()
                    .route("/in.bin", get(|| async { tokio::fs::read("in.bin").await.unwrap() }))
                    .route("/quit", get(|| async {
                        tokio::spawn(async {
                            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
                            std::process::exit(0);
                        });
                        "bye"
                    }));
                let listener = tokio::net::TcpListener::bind(("127.0.0.1", 18080)).await.unwrap();
                axum::serve(listener, router).await.unwrap();
            });
        }
    }
    println!("ok");
}
