import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1 else {
    fputs("usage: ocr <image> [image...]\n", stderr)
    exit(1)
}

for path in args.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        fputs("cannot load \(path)\n", stderr)
        continue
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([request])
    } catch {
        fputs("ocr failed \(path): \(error)\n", stderr)
        continue
    }

    let name = (path as NSString).lastPathComponent
    print("### \(name)")
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        print(String(format: "[confidence=%.3f y=%.3f x=%.3f] %@",
                     candidate.confidence, box.origin.y, box.origin.x, candidate.string))
    }
}
