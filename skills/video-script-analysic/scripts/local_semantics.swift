#!/usr/bin/env swift
import Foundation
import Vision
import AppKit

struct FrameSemantics: Codable {
    let file: String
    let time: Double
    let labels: [String]
    let faceCount: Int
    let humanCount: Int
    let faceBoxes: [[Double]]
}

struct SemanticsFile: Codable {
    let frames: [FrameSemantics]
}

func boxToArray(_ box: CGRect) -> [Double] {
    return [
        Double(box.origin.x),
        Double(box.origin.y),
        Double(box.width),
        Double(box.height)
    ]
}

let arguments = Array(CommandLine.arguments.dropFirst())
var records: [FrameSemantics] = []

for path in arguments {
    let url = URL(fileURLWithPath: path)
    guard let image = NSImage(contentsOf: url),
          let tiff = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff),
          let cgImage = bitmap.cgImage else {
        FileHandle.standardError.write(Data("cannot read image: \(path)\n".utf8))
        continue
    }

    let classifyRequest = VNClassifyImageRequest()
    let faceRequest = VNDetectFaceRectanglesRequest()
    let humanRequest = VNDetectHumanRectanglesRequest()

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([classifyRequest, faceRequest, humanRequest])
    } catch {
        FileHandle.standardError.write(Data("Vision failed for \(path): \(error)\n".utf8))
        continue
    }

    let labelObs = classifyRequest.results ?? []
    let labels = labelObs.prefix(10).map { obs -> String in
        return "\(obs.identifier):\(String(format: "%.3f", obs.confidence))"
    }
    let faceObs = faceRequest.results ?? []
    let humanObs = humanRequest.results ?? []

    var time: Double = -1
    let name = url.deletingPathExtension().lastPathComponent
    if name.hasPrefix("s_"), let n = Double(name.dropFirst(2)) {
        time = n - 1
    }

    records.append(FrameSemantics(
        file: url.lastPathComponent,
        time: time,
        labels: labels,
        faceCount: faceObs.count,
        humanCount: humanObs.count,
        faceBoxes: faceObs.map { boxToArray($0.boundingBox) }
    ))
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
let data = try encoder.encode(SemanticsFile(frames: records))
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
