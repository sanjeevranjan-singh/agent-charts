package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultChartsPath = "/charts"
	defaultNamespace  = "default"
)

type Config struct {
	FolderName  string
	ReleaseName string
	Namespace   string
	ChartsPath  string
	ValuesFile  string
	SetValues   setFlags // supports multiple --set flags
	DryRun      bool
	Wait        bool
	Timeout     string
	CreateNS    bool
	Upgrade     bool
	KubeConfig  string
	KubeContext string
}

// setFlags implements flag.Value to accumulate multiple --set flags.
// Go's flag package normally keeps only the last value for a flag;
// this type appends each occurrence so all --set values are preserved.
type setFlags []string

func (s *setFlags) String() string { return strings.Join(*s, ",") }
func (s *setFlags) Set(val string) error {
	*s = append(*s, val)
	return nil
}

func main() {
	config := parseFlags()

	if err := validateConfig(config); err != nil {
		log.Fatalf("Configuration error: %v", err)
	}

	if err := installChart(config); err != nil {
		log.Fatalf("Installation failed: %v", err)
	}

	log.Printf("Successfully installed agent chart from folder: %s", config.FolderName)
}

func parseFlags() *Config {
	config := &Config{}

	flag.StringVar(&config.FolderName, "folder", "flash-agent", "Name of the folder containing Helm chart (default: flash-agent)")
	flag.StringVar(&config.ReleaseName, "release", "", "Helm release name (defaults to folder name)")
	flag.StringVar(&config.Namespace, "namespace", defaultNamespace, "Kubernetes namespace to install into")
	flag.StringVar(&config.ChartsPath, "charts-path", defaultChartsPath, "Base path where charts are located")
	flag.StringVar(&config.ValuesFile, "values", "", "Path to custom values file")
	flag.Var(&config.SetValues, "set", "Set values on command line (can be repeated: --set key=value --set key2=value2)")
	flag.BoolVar(&config.DryRun, "dry-run", false, "Simulate installation without applying")
	flag.BoolVar(&config.Wait, "wait", true, "Wait for resources to be ready")
	flag.StringVar(&config.Timeout, "timeout", "10m", "Timeout for installation")
	flag.BoolVar(&config.CreateNS, "create-namespace", true, "Create namespace if it doesn't exist")
	flag.BoolVar(&config.Upgrade, "upgrade", true, "Use helm upgrade --install for idempotent installs (set to false to use helm install)")
	flag.StringVar(&config.KubeConfig, "kubeconfig", "", "Path to kubeconfig file")
	flag.StringVar(&config.KubeContext, "context", "", "Kubernetes context to use")

	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Usage: install-agent [options]\n\n")
		fmt.Fprintf(os.Stderr, "A tool to install agent Helm charts from the packaged repository.\n\n")
		fmt.Fprintf(os.Stderr, "Options:\n")
		flag.PrintDefaults()
		fmt.Fprintf(os.Stderr, "\nExamples:\n")
		fmt.Fprintf(os.Stderr, "  # Install flash-agent chart into flash-agent namespace\n")
		fmt.Fprintf(os.Stderr, "  install-agent -folder flash-agent -namespace flash-agent\n\n")
		fmt.Fprintf(os.Stderr, "  # Install with custom values file\n")
		fmt.Fprintf(os.Stderr, "  install-agent -folder flash-agent -values /custom/values.yaml\n\n")
		fmt.Fprintf(os.Stderr, "  # Upgrade existing release\n")
		fmt.Fprintf(os.Stderr, "  install-agent -folder flash-agent -upgrade -namespace flash-agent\n\n")
		fmt.Fprintf(os.Stderr, "  # Dry-run installation\n")
		fmt.Fprintf(os.Stderr, "  install-agent -folder flash-agent -dry-run\n")
	}

	flag.Parse()

	// Default release name to folder name if not specified
	if config.ReleaseName == "" {
		config.ReleaseName = config.FolderName
	}

	return config
}

func validateConfig(config *Config) error {
	if config.FolderName == "" {
		return fmt.Errorf("folder name is required. Use -folder flag or set a default")
	}

	chartPath := filepath.Join(config.ChartsPath, config.FolderName)
	if _, err := os.Stat(chartPath); os.IsNotExist(err) {
		return fmt.Errorf("chart folder not found: %s", chartPath)
	}

	// Check for Chart.yaml to verify it's a valid Helm chart
	chartYaml := filepath.Join(chartPath, "Chart.yaml")
	if _, err := os.Stat(chartYaml); os.IsNotExist(err) {
		return fmt.Errorf("not a valid Helm chart - Chart.yaml not found in: %s", chartPath)
	}

	// Validate values file if specified
	if config.ValuesFile != "" {
		if _, err := os.Stat(config.ValuesFile); os.IsNotExist(err) {
			return fmt.Errorf("values file not found: %s", config.ValuesFile)
		}
	}

	return nil
}

func installChart(config *Config) error {
	chartPath := filepath.Join(config.ChartsPath, config.FolderName)

	// Pre-create namespace if requested, instead of relying on Helm's --create-namespace
	// which fails with "already exists" error on upgrade --install when namespace was
	// created outside of Helm
	if config.CreateNS {
		if err := ensureNamespace(config.Namespace, config.ReleaseName); err != nil {
			log.Printf("Warning: failed to ensure namespace %s: %v", config.Namespace, err)
		}
	}

	// Adopt any pre-existing resources so Helm can manage them on upgrade --install.
	// This prevents "invalid ownership metadata" errors when resources were left behind
	// from a previous Helm release (e.g., cleanup purged the release but not the resources).
	if config.Upgrade {
		if err := adoptExistingResources(config); err != nil {
			log.Printf("Warning: failed to adopt existing resources: %v", err)
		}
	}

	// Build helm command
	var args []string

	if config.Upgrade {
		args = append(args, "upgrade", "--install")
	} else {
		args = append(args, "install")
	}

	args = append(args, config.ReleaseName, chartPath)
	args = append(args, "--namespace", config.Namespace)

	// Namespace is pre-created by ensureNamespace(), no need for --create-namespace

	if config.ValuesFile != "" {
		args = append(args, "-f", config.ValuesFile)
	}

	for _, setValue := range config.SetValues {
		args = append(args, "--set", setValue)
	}

	if config.DryRun {
		args = append(args, "--dry-run")
	}

	// NOTE: We intentionally do NOT pass --wait to Helm.
	// Helm v3.14's client-go rate limiter has a known bug that causes
	// "client rate limiter Wait returned an error: context deadline exceeded"
	// when polling pod readiness. Instead, we use kubectl rollout status below.

	if config.Timeout != "" {
		args = append(args, "--timeout", config.Timeout)
	}

	if config.KubeConfig != "" {
		args = append(args, "--kubeconfig", config.KubeConfig)
	}

	if config.KubeContext != "" {
		args = append(args, "--kube-context", config.KubeContext)
	}

	log.Printf("Executing: helm %s", strings.Join(args, " "))

	cmd := exec.Command("helm", args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return err
	}

	// If --wait was requested, use kubectl rollout status instead of Helm's
	// built-in wait which suffers from client-go rate limiter bugs in v3.14
	if config.Wait {
		if err := waitForDeployments(config.Namespace, config.Timeout); err != nil {
			return fmt.Errorf("deployments not ready: %w", err)
		}
	}

	return nil
}

// waitForDeployments waits for all deployments in the namespace to be ready
// using kubectl rollout status, which doesn't suffer from Helm's rate limiter bug.
func waitForDeployments(namespace, timeout string) error {
	if timeout == "" {
		timeout = "10m"
	}

	log.Printf("Waiting for all deployments in namespace %s to be ready (timeout: %s)...", namespace, timeout)

	// Get list of deployments
	listCmd := exec.Command("kubectl", "get", "deployments", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}")
	out, err := listCmd.Output()
	if err != nil {
		return fmt.Errorf("failed to list deployments: %w", err)
	}

	deployments := strings.Fields(string(out))
	if len(deployments) == 0 {
		log.Printf("No deployments found in namespace %s, skipping wait", namespace)
		return nil
	}

	log.Printf("Found %d deployments: %s", len(deployments), strings.Join(deployments, ", "))

	// Wait for each deployment
	for _, dep := range deployments {
		log.Printf("Waiting for deployment %s...", dep)
		waitCmd := exec.Command("kubectl", "rollout", "status", "deployment/"+dep,
			"-n", namespace, "--timeout="+timeout)
		waitCmd.Stdout = os.Stdout
		waitCmd.Stderr = os.Stderr
		if err := waitCmd.Run(); err != nil {
			return fmt.Errorf("deployment %s not ready: %w", dep, err)
		}
		log.Printf("Deployment %s is ready", dep)
	}

	log.Printf("All deployments in namespace %s are ready", namespace)
	return nil
}

// ensureNamespace creates the namespace if it doesn't already exist and ensures
// it has the required Helm ownership labels and annotations so Helm can adopt it.
func ensureNamespace(namespace, releaseName string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// Check if namespace exists
	checkCmd := exec.Command("kubectl", "get", "namespace", namespace)
	if err := checkCmd.Run(); err != nil {
		// Create namespace
		log.Printf("Creating namespace: %s", namespace)
		createCmd := exec.CommandContext(ctx, "kubectl", "create", "namespace", namespace)
		createCmd.Stdout = os.Stdout
		createCmd.Stderr = os.Stderr
		if err := createCmd.Run(); err != nil {
			return fmt.Errorf("failed to create namespace: %w", err)
		}
	} else {
		log.Printf("Namespace %s already exists", namespace)
	}

	// Add Helm ownership labels and annotations so Helm can adopt the namespace
	log.Printf("Labeling namespace %s for Helm ownership", namespace)
	labelCmd := exec.CommandContext(ctx, "kubectl", "label", "namespace", namespace,
		"app.kubernetes.io/managed-by=Helm", "--overwrite")
	labelCmd.Stdout = os.Stdout
	labelCmd.Stderr = os.Stderr
	if err := labelCmd.Run(); err != nil {
		return fmt.Errorf("failed to label namespace: %w", err)
	}

	annotateCmd := exec.CommandContext(ctx, "kubectl", "annotate", "namespace", namespace,
		fmt.Sprintf("meta.helm.sh/release-name=%s", releaseName),
		fmt.Sprintf("meta.helm.sh/release-namespace=%s", namespace),
		"--overwrite")
	annotateCmd.Stdout = os.Stdout
	annotateCmd.Stderr = os.Stderr
	if err := annotateCmd.Run(); err != nil {
		return fmt.Errorf("failed to annotate namespace: %w", err)
	}

	return nil
}

// adoptExistingResources uses `helm template` to discover all resources that
// the chart will create, then checks if any already exist in the cluster without
// proper Helm ownership metadata. If found, it labels and annotates them so that
// `helm upgrade --install` can adopt them instead of failing with
// "invalid ownership metadata" errors.
//
// This handles the case where a previous experiment run created resources via Helm,
// but the cleanup step (or manual intervention) purged the Helm release without
// deleting the underlying Kubernetes resources. On the next run, Helm sees orphaned
// resources it can't claim ownership of.
func adoptExistingResources(config *Config) error {
	chartPath := filepath.Join(config.ChartsPath, config.FolderName)

	// Build helm template command with the same args as the actual install
	args := []string{"template", config.ReleaseName, chartPath, "--namespace", config.Namespace}

	if config.ValuesFile != "" {
		args = append(args, "-f", config.ValuesFile)
	}

	for _, setValue := range config.SetValues {
		args = append(args, "--set", setValue)
	}

	log.Printf("Discovering chart resources via: helm %s", strings.Join(args, " "))

	cmd := exec.Command("helm", args...)
	out, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("helm template failed: %w", err)
	}

	// Parse the rendered manifests to extract resource kind/name/namespace
	resources := parseHelmTemplateOutput(string(out))
	if len(resources) == 0 {
		log.Printf("No resources discovered from chart template")
		return nil
	}

	log.Printf("Discovered %d resources from chart template", len(resources))

	adopted := 0
	for _, res := range resources {
		if adoptResource(res, config.ReleaseName, config.Namespace) {
			adopted++
		}
	}

	if adopted > 0 {
		log.Printf("Adopted %d pre-existing resources for Helm release %s", adopted, config.ReleaseName)
	}

	return nil
}

// k8sResource represents a Kubernetes resource extracted from Helm template output.
type k8sResource struct {
	Kind      string
	Name      string
	Namespace string // empty for cluster-scoped resources
}

// parseHelmTemplateOutput parses multi-document YAML from `helm template` and
// extracts the kind, name, and namespace of each resource.
func parseHelmTemplateOutput(output string) []k8sResource {
	docs := strings.Split(output, "---")
	var resources []k8sResource

	for _, doc := range docs {
		doc = strings.TrimSpace(doc)
		if doc == "" {
			continue
		}

		var kind, name, namespace string
		inMetadata := false

		for _, line := range strings.Split(doc, "\n") {
			trimmed := strings.TrimSpace(line)

			// Skip blank lines and comment lines (e.g., "# Source: ...")
			if trimmed == "" || strings.HasPrefix(trimmed, "#") {
				continue
			}

			// Top-level kind field (not indented)
			if strings.HasPrefix(trimmed, "kind:") && !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t") {
				kind = strings.TrimSpace(strings.TrimPrefix(trimmed, "kind:"))
				continue
			}

			// Enter metadata section (top-level)
			if trimmed == "metadata:" && !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t") {
				inMetadata = true
				continue
			}

			// Exit metadata section when we hit another top-level key
			if inMetadata && !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t") {
				inMetadata = false
			}

			// Inside metadata, extract name and namespace (first-level indented keys)
			if inMetadata {
				// Only match direct children of metadata (2-space indent),
				// not nested keys like annotations/labels children
				if strings.HasPrefix(trimmed, "name:") && !strings.Contains(trimmed, "/") {
					// Make sure it's a direct child (indented 2 spaces, not deeper)
					indent := len(line) - len(strings.TrimLeft(line, " \t"))
					if indent <= 4 {
						name = strings.TrimSpace(strings.TrimPrefix(trimmed, "name:"))
						name = strings.Trim(name, "\"'")
					}
				}
				if strings.HasPrefix(trimmed, "namespace:") {
					namespace = strings.TrimSpace(strings.TrimPrefix(trimmed, "namespace:"))
					namespace = strings.Trim(namespace, "\"'")
				}
			}
		}

		if kind != "" && name != "" {
			resources = append(resources, k8sResource{Kind: kind, Name: name, Namespace: namespace})
		}
	}

	return resources
}

// adoptResource checks if a Kubernetes resource exists and, if so, ensures it has
// the correct Helm ownership labels and annotations. Returns true if the resource
// was adopted (i.e., it existed and was labeled).
func adoptResource(res k8sResource, releaseName, releaseNamespace string) bool {
	resourceType := strings.ToLower(res.Kind)

	// Check if the resource exists
	var getArgs []string
	if res.Namespace != "" {
		getArgs = []string{"get", resourceType, res.Name, "-n", res.Namespace, "--no-headers", "--ignore-not-found"}
	} else {
		getArgs = []string{"get", resourceType, res.Name, "--no-headers", "--ignore-not-found"}
	}

	checkCmd := exec.Command("kubectl", getArgs...)
	out, err := checkCmd.Output()
	if err != nil || strings.TrimSpace(string(out)) == "" {
		// Resource doesn't exist — nothing to adopt
		return false
	}

	log.Printf("Adopting existing %s/%s for Helm release %s", res.Kind, res.Name, releaseName)

	// Add Helm ownership label
	var labelArgs []string
	if res.Namespace != "" {
		labelArgs = []string{"label", resourceType, res.Name, "-n", res.Namespace,
			"app.kubernetes.io/managed-by=Helm", "--overwrite"}
	} else {
		labelArgs = []string{"label", resourceType, res.Name,
			"app.kubernetes.io/managed-by=Helm", "--overwrite"}
	}
	labelCmd := exec.Command("kubectl", labelArgs...)
	labelCmd.Stdout = os.Stdout
	labelCmd.Stderr = os.Stderr
	if err := labelCmd.Run(); err != nil {
		log.Printf("Warning: failed to label %s/%s: %v", res.Kind, res.Name, err)
	}

	// Add Helm ownership annotations
	var annotateArgs []string
	if res.Namespace != "" {
		annotateArgs = []string{"annotate", resourceType, res.Name, "-n", res.Namespace,
			fmt.Sprintf("meta.helm.sh/release-name=%s", releaseName),
			fmt.Sprintf("meta.helm.sh/release-namespace=%s", releaseNamespace),
			"--overwrite"}
	} else {
		annotateArgs = []string{"annotate", resourceType, res.Name,
			fmt.Sprintf("meta.helm.sh/release-name=%s", releaseName),
			fmt.Sprintf("meta.helm.sh/release-namespace=%s", releaseNamespace),
			"--overwrite"}
	}
	annotateCmd := exec.Command("kubectl", annotateArgs...)
	annotateCmd.Stdout = os.Stdout
	annotateCmd.Stderr = os.Stderr
	if err := annotateCmd.Run(); err != nil {
		log.Printf("Warning: failed to annotate %s/%s: %v", res.Kind, res.Name, err)
	}

	return true
}

// ListAvailableCharts lists all available charts in the charts path
func ListAvailableCharts(chartsPath string) ([]string, error) {
	var charts []string

	entries, err := os.ReadDir(chartsPath)
	if err != nil {
		return nil, fmt.Errorf("failed to read charts directory: %w", err)
	}

	for _, entry := range entries {
		if entry.IsDir() {
			chartYaml := filepath.Join(chartsPath, entry.Name(), "Chart.yaml")
			if _, err := os.Stat(chartYaml); err == nil {
				charts = append(charts, entry.Name())
			}
		}
	}

	return charts, nil
}
