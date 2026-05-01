#!/usr/bin/env swift
// ocr_vision.swift — Extract text from an image using macOS Vision framework.
// Usage: swift ocr_vision.swift /path/to/image.png
// Prints recognized text to stdout, one line per text block.
// No dependencies — uses built-in macOS Vision framework.

import Foundation
import Vision

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: swift ocr_vision.swift <image-path>\n", stderr)
    exit(1)
}

let imagePath = CommandLine.arguments[1]
guard let imageURL = URL(string: "file://\(imagePath)"),
      FileManager.default.fileExists(atPath: imagePath) else {
    fputs("ERROR: file not found: \(imagePath)\n", stderr)
    exit(1)
}

guard let imageSource = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let cgImage = CGImageSourceCreateImageAtIndex(imageSource, 0, nil) else {
    fputs("ERROR: unable to load image: \(imagePath)\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fputs("ERROR: Vision OCR failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}

guard let observations = request.results else {
    exit(0)
}

for observation in observations {
    if let candidate = observation.topCandidates(1).first {
        print(candidate.string)
    }
}
