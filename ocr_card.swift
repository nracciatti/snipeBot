import Foundation
import Vision

let image = FileHandle.standardInput.readDataToEndOfFile()
guard !image.isEmpty else {
    fputs("empty image\n", stderr)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["en-US"]
request.usesLanguageCorrection = false

do {
    try VNImageRequestHandler(data: image).perform([request])
    for observation in request.results ?? [] {
        if let candidate = observation.topCandidates(1).first {
            print(candidate.string)
        }
    }
} catch {
    fputs("OCR failed: \(error)\n", stderr)
    exit(1)
}
