import Foundation
import Vision
import AppKit

var outLines: [String] = []
for path in CommandLine.arguments.dropFirst() {
    outLines.append("=== FILE: \(path)")
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        outLines.append("<unreadable>")
        continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([req]) } catch {
        outLines.append("<ocr-failed: \(error)>")
        continue
    }
    for o in req.results ?? [] {
        if let c = o.topCandidates(1).first { outLines.append(c.string) }
    }
}
print(outLines.joined(separator: "\n"))
